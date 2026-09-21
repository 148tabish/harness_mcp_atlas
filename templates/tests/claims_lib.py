"""Claim-coverage machinery used by tests/test_outputs.py.

Deterministic claim-coverage grading in the MCP-Atlas shape (arXiv 2602.00933 §3.2):
atomic claims, each scored fulfilled 1.0 /
partially_fulfilled 0.5 / not_fulfilled 0.0, task coverage = mean claim score, pass at
coverage >= 0.75. No LLM. Figures and identifiers are matched exactly (or to a stated
physical tolerance); TF-IDF cosine similarity is used for the three things it is fit
for -- finding the sentence that addresses a claim, finding the sentence in which the
run states its headline figure, and topical coverage of reasoning claims paired with
anchor terms -- and never for deciding whether a figure is right, because the guarded
wrong totals of these tasks are string-near-identical to the right ones.

The task-specific part (claims table, tests) lives in test_outputs.py beside it.
Stdlib only.

Inputs (env; same contract as test_outputs.py):
  ATLAS_TRAJECTORY      /tmp/agent_trajectory.json from test.sh step 1
                        ({steps, final_message}) or a raw Harbor ATIF trajectory.json
  ATLAS_OUTPUT_ROOTS    os.pathsep list of dirs holding delivered files
                        (default /workspace and /logs/artifacts)
  ATLAS_ORACLE / ATLAS_OLD_ENV                  fixture overrides
  AUTH_CLAIMS_PATH      where coverage() writes its JSON
                        (default /logs/verifier/claims_coverage.json)
"""
import csv as _csv
import json as _json
import math as _math
import os as _os
import pathlib as _pathlib
import re as _re
from collections import Counter as _Counter

_CACHE: dict = {}
_SKIP_DIRS = {'data', 'node_modules', '__pycache__', 'logs', 'agent', 'verifier', 'sessions', 'work', '.git'}


# ================================================================== FIXTURES
def load_fixture(env_name, file_name, here):
    """JSON fixture by env override, then beside the caller, then /tests."""
    for cand in (_os.environ.get(env_name), _pathlib.Path(here) / file_name, _pathlib.Path('/tests') / file_name):
        if not cand:
            continue
        try:
            return _json.loads(_pathlib.Path(cand).read_text())
        except (OSError, ValueError):
            continue
    return {}


# ================================================================ TRAJECTORY
def _parse_json(raw):
    try:
        return _json.loads(raw) if isinstance(raw, str) else raw
    except (_json.JSONDecodeError, TypeError):
        return {'text': str(raw)}


def _text_of(raw):
    if isinstance(raw, list) and raw:
        return raw[0].get('text', '') if isinstance(raw[0], dict) else str(raw[0])
    return raw


def atif(doc):
    """Harbor ATIF -> (steps, final_message), the reading tests/test.sh step 1 makes, so a
    raw trajectory.json and test.sh's rebuilt file grade alike."""
    out, by_id, final = [], {}, ''
    for st in doc.get('steps') or []:
        if st.get('source') != 'agent':
            continue
        msg = st.get('message')
        if isinstance(msg, list):
            msg = ' '.join(p.get('text') or '' for p in msg if isinstance(p, dict) and p.get('type') == 'text')
        if isinstance(msg, str) and msg.strip():
            final = msg
        for tc in st.get('tool_calls') or []:
            cid = tc.get('tool_call_id') or ''
            if cid and cid in by_id:
                continue
            entry = {'tool': tc.get('function_name', ''), 'arguments': tc.get('arguments') or {}, 'response': None}
            out.append(entry)
            if cid:
                by_id[cid] = entry
        results = (st.get('observation') or {}).get('results') or []
        for r in results:
            target = by_id.get(r.get('source_call_id'))
            if target is None and len(results) == 1 and out and out[-1]['response'] is None:
                target = out[-1]
            if target is not None and target['response'] is None:
                target['response'] = _parse_json(_text_of(r.get('content')))
    return out, final


def traj() -> dict:
    if 'traj' not in _CACHE:
        path = _os.environ.get('ATLAS_TRAJECTORY') or _os.environ.get('MCPATLAS_TRAJECTORY')
        doc = {}
        if path and _os.path.exists(path):
            try:
                doc = _json.loads(open(path).read())
            except ValueError:
                doc = {}
        if isinstance(doc, dict) and 'final_message' not in doc and doc.get('steps') \
                and any(isinstance(s, dict) and 'source' in s for s in doc['steps']):
            steps, final = atif(doc)
            doc = {'steps': steps, 'final_message': final}
        _CACHE['traj'] = doc if isinstance(doc, dict) else {}
    return _CACHE['traj']


def steps() -> list:
    return [s for s in (traj().get('steps') or []) if isinstance(s, dict)]


def final() -> str:
    return str(traj().get('final_message') or '')


_NAME_RE = _re.compile(r'^(Light[A-Za-z0-9]+)[_.:\-]+(.+)$')


def split_tool(name) -> tuple:
    """'mcp__LightBudget__update_transaction' -> ('LightBudget', 'update_transaction')."""
    bare = str(name or '').split('::')[-1]
    if bare.startswith('mcp__'):
        parts = bare.split('__')
        if len(parts) >= 3:
            return parts[1], '__'.join(parts[2:])
    m = _NAME_RE.match(bare)
    if m:
        return m.group(1), m.group(2)
    return '', bare


def args(step) -> dict:
    a = step.get('arguments')
    return a if isinstance(a, dict) else {}


def accepted(step) -> bool:
    """The server said ok. A status-less error envelope is not an accepted call."""
    r = step.get('response')
    return isinstance(r, dict) and r.get('status') == 'ok'


def mcp(server, *tools):
    """(index, step) for every ACCEPTED call of one of `tools` on `server`."""
    out = []
    for i, s in enumerate(steps()):
        srv, tool = split_tool(s.get('tool'))
        if srv == server and (not tools or tool in tools) and accepted(s):
            out.append((i, s))
    return out


def run_invalid_reason():
    """A run the infrastructure killed is not a run the agent failed."""
    if not steps():
        return 'no tool calls recorded'
    last = final().lower()
    for mark in ('hit your session limit', 'usage limit', 'rate limit', 'credit balance is too low'):
        if mark in last:
            return f'agent process stopped by the platform ({mark})'
    return None


def install_skip_fixture(pytest_module, namespace: dict):
    """Give the calling test module the autouse fixture test_outputs.py has: invalid
    runs skip rather than fail, unless ATLAS_SCORE_INVALID_RUNS=1."""
    if pytest_module is None:
        return

    @pytest_module.fixture(autouse=True)
    def _skip_infrastructure_deaths():
        if _os.environ.get('ATLAS_SCORE_INVALID_RUNS') != '1':
            reason = run_invalid_reason()
            if reason and steps():
                pytest_module.skip(f'INVALID RUN: {reason}')

    namespace['_skip_infrastructure_deaths'] = _skip_infrastructure_deaths


# ================================================================== NUMBERS
_UNITS = {w: i for i, w in enumerate(['zero', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight',
                                      'nine', 'ten', 'eleven', 'twelve', 'thirteen', 'fourteen', 'fifteen',
                                      'sixteen', 'seventeen', 'eighteen', 'nineteen'])}
_TENS = {'twenty': 20, 'thirty': 30, 'forty': 40, 'fifty': 50, 'sixty': 60, 'seventy': 70, 'eighty': 80, 'ninety': 90}
_SCALES = {'hundred': 100, 'thousand': 1_000, 'million': 1_000_000, 'billion': 1_000_000_000}
_WORDNUM_RE = _re.compile(
    r'\b((?:(?:' + '|'.join(list(_UNITS) + list(_TENS) + list(_SCALES) + ['and']) + r')[\s,\-]+)+'
    r'(?:' + '|'.join(list(_UNITS) + list(_TENS) + list(_SCALES)) + r'))\b', _re.I)
_SINGLE_WORD_RE = _re.compile(r'\b(' + '|'.join(list(_UNITS)[1:] + list(_TENS)) + r')\b', _re.I)
DIGITS_RE = _re.compile(r'(?<![\w.])(?<!\d:)(?<![A-Za-z]-)(?<!\d-)((?:(?<=[\s(=:>\[|])[-\u2212\u2013])?)[$£]?\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?(?![:\d])[\u00bc\u00bd\u00be]?\s*(billion|million|thousand|bn|mn|m|k)?\b', _re.I)
_SUFFIX = {'billion': 1e9, 'bn': 1e9, 'million': 1e6, 'mn': 1e6, 'm': 1e6, 'thousand': 1e3, 'k': 1e3}
YEARS = range(1800, 2101)


def words_to_number(phrase: str):
    """'fifty-two million, three hundred and seventy-three thousand' -> 52373000."""
    total, group, seen = 0, 0, False
    for w in _re.split(r'[\s,\-]+', phrase.lower()):
        if not w or w == 'and':
            continue
        if w in _UNITS:
            group += _UNITS[w]; seen = True
        elif w in _TENS:
            group += _TENS[w]; seen = True
        elif w == 'hundred':
            group = (group or 1) * 100; seen = True
        elif w in _SCALES:
            total += (group or 1) * _SCALES[w]; group = 0; seen = True
        else:
            return None
    return total + group if seen else None


def numbers_in(text: str, words: bool = True, small_words: bool = False) -> set:
    """Every figure a reader would take off the text, as a float: digits with or without
    thousands separators, decimals, 'X million' / '$50m' forms, and English number words
    (multi-word phrases always; single words like 'seven' only when small_words=True,
    because 'one' is everywhere in prose). A signed figure contributes both its magnitude
    and its signed value, so a bridge step written -27,853,000 satisfies a claim for
    27,853,000 while a claim for -0.14 still needs the minus."""
    out = set()
    for m in DIGITS_RE.finditer(text):
        sign, whole, frac, suf = m.group(1), m.group(2).replace(',', ''), m.group(3), (m.group(4) or '').lower()
        raw = m.group(0)
        # '$50m' / '52.4m' are millions; '8.35 m' (a space before the m, no currency) is metres
        if suf == 'm' and _re.search(r'\d\s+m$', raw, _re.I) and not _re.search(r'[$£]', raw):
            suf = ''
        val = float(whole + ('.' + frac if frac else ''))
        if suf:
            val *= _SUFFIX[suf]
        out.add(round(val, 6))                 # magnitude always; bridge steps print as -27,853,000
        if sign:
            out.add(round(-val, 6))            # and the signed value, for claims that care about sign
    if words:
        for m in _WORDNUM_RE.finditer(text):
            v = words_to_number(m.group(1))
            if v is not None and (v >= 1000 or small_words):
                out.add(float(v))
        if small_words:
            for m in _SINGLE_WORD_RE.finditer(text):
                v = words_to_number(m.group(1))
                if v:
                    out.add(float(v))
    return out


def has(nums, value, tol=0.0, absolute=False) -> bool:
    """Is `value` among `nums`, within `tol` (0 = exact)? absolute=True ignores sign."""
    for n in nums:
        a, b = (abs(n), abs(value)) if absolute else (n, value)
        if abs(a - b) <= tol + 1e-9:
            return True
    return False


def fmt(n) -> str:
    return f'{int(n):,}' if float(n).is_integer() else f'{n:g}'


def as_float(v):
    try:
        return float(str(v).replace(',', '').replace('$', '').replace('£', '').strip())
    except (TypeError, ValueError):
        return None


def norm(value) -> str:
    """Channel A's key/value normaliser: casefold, alphanumerics only."""
    return ''.join(c for c in str(value).casefold() if c.isalnum())


# ============================================================ COSINE MACHINERY
_STOP = set('a an and are as at be by for from has have in is it its of on or that the this to was were will with '
            'which who whom whose than then there these those they them their our we you your i me my he she his her '
            'not no nor so if but into onto over under out off up down about above below between across after before '
            'do does did done been being also just only very such same other each every any all both few more most '
            'own too can could may might shall should would must here where when how what why'.split())


def tokens(text: str) -> list:
    text = _re.sub(r'(?<=\d),(?=\d{3}\b)', '', text.lower())        # 52,373,000 -> one token
    text = _re.sub(r'[*_`#>|]', ' ', text)                          # markdown furniture
    words = [w for w in _re.findall(r'[a-z0-9$£][a-z0-9$£\-]*', text) if w not in _STOP and len(w) > 1]
    return words + [f'{a}_{b}' for a, b in zip(words, words[1:])]   # unigrams + bigrams


def sentences(text: str) -> list:
    """Chunks a claim can be matched against: sentences, table rows and bullets, plus
    every pair of neighbours so a claim spanning two sentences still lands."""
    text = text.replace('\r', '')
    raw = []
    for para in _re.split(r'\n{2,}|\n(?=\s*[-*|•]|\s*\d+[.)]\s)', text):
        for line in para.split('\n'):
            raw.extend(_re.split(r'(?<=[.!?;:])(?<!\ds\.)(?<!\dd\.)(?<!\dl\.)(?<! l\.)\s+(?=[A-Z$£*("\'\-•\d])', line))
    sents = [s.strip(' *-•|\t') for s in raw if len(tokens(s)) >= 3]
    return sents + [f'{a} {b}' for a, b in zip(sents, sents[1:])]


class TfIdf:
    """TF-IDF vectors (sublinear tf, smoothed idf, L2 norm) over a chunk list.
    Deterministic, dependency-free."""

    def __init__(self, chunks):
        self.chunks = list(chunks)
        docs = [_Counter(tokens(c)) for c in self.chunks]
        df = _Counter(t for d in docs for t in d)
        n = len(docs) + 1
        self.idf = {t: _math.log((n + 1) / (c + 1)) + 1.0 for t, c in df.items()}
        self.vecs = [self.vec(d) for d in docs]

    def vec(self, counts):
        v = {t: (1 + _math.log(c)) * self.idf.get(t, _math.log(len(self.chunks) + 2) + 1.0) for t, c in counts.items()}
        nrm = _math.sqrt(sum(x * x for x in v.values())) or 1.0
        return {t: x / nrm for t, x in v.items()}

    def sim(self, qvec, i) -> float:
        v = self.vecs[i]
        return sum(x * v.get(t, 0.0) for t, x in qvec.items())

    def best(self, query: str):
        """(similarity, chunk) of the chunk most similar to `query`; (0.0, '') if none."""
        top = self.topk(query, 1)
        return top[0] if top else (0.0, '')

    def topk(self, query: str, k: int = 5) -> list:
        """[(similarity, chunk)] for the k chunks most similar to `query`, best first."""
        q = self.vec(_Counter(tokens(query)))
        scored = [(self.sim(q, i), c) for i, c in enumerate(self.chunks)]
        scored = [t for t in scored if t[0] > 0.0]
        scored.sort(key=lambda t: -t[0])
        return scored[:k]


def cosine(a: str, b: str) -> float:
    """Cosine similarity of two texts under the same TF-IDF weighting."""
    model = TfIdf([a, b])
    va, vb = model.vecs
    return sum(x * vb.get(t, 0.0) for t, x in va.items())


def index(text: str = None) -> TfIdf:
    """TF-IDF index over the final answer's sentences (cached), or over `text`."""
    key = ('idx', text if text is not None else '<final>')
    if key not in _CACHE:
        _CACHE[key] = TfIdf(sentences(final() if text is None else text))
    return _CACHE[key]


# SIM_PART is the bar for "this sentence is about the claim"; SIM_FULL is the
# higher bar a claim with no figures or anchor terms of its own must clear on
# similarity alone.
SIM_FULL, SIM_PART = 0.22, 0.12


# ================================================================== CLAIMS
def _terms_present(text_lc: str, terms) -> bool:
    return any(t.lower() in text_lc for t in (terms or []))


def _numbers_ok(claim, nums_answer, nums_evidence):
    """Which of the claim's figures are absent, honouring tolerance / sign / scope."""
    tol = claim.get('tolerance', 0.0)
    absolute = bool(claim.get('abs_numbers'))
    pool = nums_evidence if claim.get('numbers_scope') == 'evidence' else nums_answer
    return [n for n in claim.get('numbers', []) if not has(pool, n, tol, absolute)]


def score_claim(claim: dict, answer: str, idx: TfIdf = None) -> dict:
    """One claim against an answer -> a per-claim record, plus the
    evidence sentence and its similarity. The facts (figures, anchor terms) carry the
    verdict; similarity says whether some sentence is actually about the claim rather
    than the anchors lying scattered through the text.

    Claim fields: id, text, weight; numbers [+ tolerance, abs_numbers, numbers_scope
    'answer'|'evidence']; all_terms (every one present), any_terms / any_terms2 (one of
    each present), any_regex (one pattern matches the lower-cased answer); guard_numbers (a figure in the evidence sentence that contradicts the
    claim); reject_terms (words in the evidence sentence that turn an assertion into a
    rejection, e.g. 'superseded', so 'the draft's 0.5 was set aside' does not count as
    stating 0.5).
      guard number in the evidence sentence               -> not_fulfilled
      facts intact and a sentence about it (>= SIM_PART)  -> fulfilled
      no facts declared, similarity alone >= SIM_FULL     -> fulfilled
      facts intact but no such sentence, or the sentence
        without the facts                                 -> partially_fulfilled
      otherwise                                           -> not_fulfilled
    """
    idx = idx or index(answer if answer != final() else None)
    lc = answer.lower().replace('\u2019', "'").replace('\u2018', "'").replace('\u201c', '"').replace('\u201d', '"')
    small = bool(claim.get('small_words'))
    nums = numbers_in(answer, small_words=small)
    has_facts = any(claim.get(k) for k in ('numbers', 'all_terms', 'any_terms', 'any_terms2', 'any_regex'))

    def facts(chunk):
        """(facts_ok, missing_numbers, chunk_numbers) for one candidate evidence sentence."""
        chunk_nums = numbers_in(chunk, small_words=small) if chunk else set()
        missing = _numbers_ok(claim, nums, chunk_nums)
        ok = not missing
        chunk_l = chunk.lower().replace('\u2019', "'") if chunk else ''
        scope = chunk_l if claim.get('terms_scope') == 'evidence' else lc      # where anchor terms must sit
        if claim.get('all_terms') and not all(t.lower() in scope for t in claim['all_terms']):
            ok = False
        for key in ('any_terms', 'any_terms2'):
            if claim.get(key) and not _terms_present(scope, claim[key]):
                ok = False
        if claim.get('any_regex') and not any(_re.search(rx, scope, _re.I | _re.S) for rx in claim['any_regex']):
            ok = False
        return ok, missing, chunk_nums

    # Evidence retrieval is fact-aware: among the five sentences most like the claim, the
    # first that carries the claim's own figures and terms is the evidence; failing that,
    # the most similar. An LLM judge reads for the sentence with the numbers in it, and a
    # short table row must not outrank the sentence that states the figure.
    cands = idx.topk(claim['text'], 5) or [(0.0, '')]
    sim, chunk = cands[0]
    facts_ok, missing_nums, chunk_nums = facts(chunk)
    if has_facts and not facts_ok:
        for s2, c2 in cands[1:]:
            ok2, miss2, nums2 = facts(c2)
            if ok2 and s2 >= claim.get('min_sim', SIM_PART):
                sim, chunk, facts_ok, missing_nums, chunk_nums = s2, c2, ok2, miss2, nums2
                break
    chunk_lc = chunk.lower().replace('\u2019', "'") if chunk else ''
    guarded = [n for n in claim.get('guard_numbers', []) if has(chunk_nums, n, claim.get('tolerance', 0.0))]
    guarded += [t for t in (claim.get('guard_terms') or []) if t.lower() in chunk_lc]
    rejected = bool(chunk) and _terms_present(chunk_lc, claim.get('reject_terms'))

    if guarded:
        outcome, score = 'not_fulfilled', 0.0
    elif rejected and claim.get('numbers'):
        outcome, score = ('partially_fulfilled', 0.5) if facts_ok else ('not_fulfilled', 0.0)
    elif (has_facts and facts_ok and sim >= claim.get('min_sim', SIM_PART)) or (not has_facts and sim >= SIM_FULL):
        outcome, score = 'fulfilled', 1.0
    elif (has_facts and facts_ok) or sim >= SIM_PART:
        outcome, score = 'partially_fulfilled', 0.5
    else:
        outcome, score = 'not_fulfilled', 0.0

    why = []
    if missing_nums:
        why.append('figures absent: ' + ', '.join(fmt(n) for n in missing_nums))
    if guarded:
        why.append('evidence sentence carries a guarded figure or term: ' + ', '.join(fmt(n) if isinstance(n, (int, float)) else str(n) for n in guarded))
    if rejected:
        why.append('evidence sentence rejects rather than states')
    if not facts_ok and not missing_nums:
        why.append('anchor terms absent')
    why.append(f'evidence similarity {sim:.2f}')
    return {'claim': claim['text'], 'id': claim['id'], 'weight': claim.get('weight', 1), 'score': score,
            'coverage_outcome': outcome, 'covered': True if score == 1.0 else ('partial' if score else False),
            'reason': '; '.join(why), 'evidence': chunk[:300], 'similarity': round(sim, 3)}


def coverage(claims, answer: str = None, extra: dict = None, post=None) -> dict:
    """Score every claim once, write the coverage JSON, cache it, with a weighted
    mean. `post(per_claim_list)`
    lets a task adjust records that depend on the trajectory too (e.g. a claim that the
    live schedule was corrected cannot be met when no live update exists)."""
    if 'coverage' in _CACHE:
        return _CACHE['coverage']
    answer = final() if answer is None else answer
    per = [score_claim(c, answer) for c in claims]
    if post:
        post(per)
    n = len(per)
    tot = sum(p['score'] for p in per)
    wsum = sum(p['weight'] for p in per) or 1
    doc = {
        'per_claim': per,
        'coverage_score': round(tot / n, 3) if n else None,
        'weighted_coverage': round(sum(p['score'] * p['weight'] for p in per) / wsum, 3) if n else None,
        'total_claims': n,
        'fully_covered_claims': sum(1 for p in per if p['score'] == 1.0),
        'partially_covered_claims': sum(1 for p in per if p['score'] == 0.5),
        'pass_0.75': bool(n and tot / n >= 0.75),
        'explanation': 'Deterministic claim coverage: exact/toleranced figures + TF-IDF cosine evidence retrieval. '
                       'Lenient on reasoning errors an LLM judge would catch; strict on figures.',
        'similarity_thresholds': {'fulfilled': SIM_FULL, 'partial': SIM_PART},
    }
    doc.update(extra or {})
    dest = _pathlib.Path(_os.environ.get('AUTH_CLAIMS_PATH', '/logs/verifier/claims_coverage.json'))
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(_json.dumps(doc, indent=2, default=str))
    except OSError:
        pass
    _CACHE['coverage'] = doc
    return doc


# ================================================================= HEADLINE
REJECTING_BEFORE = _re.compile(r'\b(not|rather than|instead of|rejected|against|was|last year\'?s?|less|from|versus|vs\.?|'
                               r'would (?:be|give|have)|lands? on|would land)\b[^0-9$£]{0,30}$', _re.I)


def stated_figures(sentence: str, min_value=1000, exclude_years=True) -> list:
    """Money-scale figures the sentence ASSERTS, leaving out the ones it names to reject:
    "the return is $52,448,000, not $52,373,000" states the first and rejects the second.
    A figure is rejected when the words just before it are a negation or a contrast
    ("not", "rather than", "instead of", "was", "last year's", "less", "against")."""
    def keep(v):
        return v >= min_value and not (exclude_years and float(v).is_integer() and int(v) in YEARS)
    out = []
    for m in DIGITS_RE.finditer(sentence):
        vals = {v for v in numbers_in(m.group(0), words=False) if keep(v)}
        if vals and not REJECTING_BEFORE.search(sentence[max(0, m.start() - 40):m.start()]):
            out.extend(sorted(vals))
    for m in _WORDNUM_RE.finditer(sentence):
        v = words_to_number(m.group(1))
        if v is not None and keep(v) and not REJECTING_BEFORE.search(sentence[max(0, m.start() - 40):m.start()]):
            out.append(float(v))
    return sorted(set(out))


def headline(query: str, boost_terms=(), demote_terms=(), min_value=1000, answer: str = None) -> dict:
    """The figure the run STATES as its answer to `query`: the sentence (carrying a
    money-scale figure) most similar to the query, with bold and the task's own headline
    words counted for, bridge-step words counted against; then the figures that sentence
    asserts. Guards are scoped to this, so a bridge that prints the gross total on its
    way down is not punished for showing its working."""
    key = ('headline', query, answer is None)
    if key in _CACHE:
        return _CACHE[key]
    answer = final() if answer is None else answer

    def money(text):
        return {n for n in numbers_in(text) if n >= min_value and not (float(n).is_integer() and int(n) in YEARS)}

    sents = [s for s in sentences(answer) if money(s)]
    bold = [_re.sub(r'\s+', ' ', b).strip(' *') for b in _re.findall(r'\*\*(.+?)\*\*', answer, _re.S)]
    best, arg = -1.0, ''
    if sents:
        model = TfIdf(sents)
        q = model.vec(_Counter(tokens(query)))
        for i, chunk in enumerate(model.chunks):
            s = model.sim(q, i)
            lc = _re.sub(r'\s+', ' ', chunk.lower())
            core = lc[:40]
            s += 0.15 * any(core in b.lower() or (b.lower() in lc and money(b)) for b in bold)
            s += 0.10 * any(t in lc for t in boost_terms)
            s -= 0.10 * any(t in lc for t in demote_terms)
            if s > best:
                best, arg = s, chunk
    all_figures = sorted(money(arg)) if arg else []
    stated = stated_figures(arg, min_value) if arg else []
    _CACHE[key] = {'sentence': arg[:400], 'figures': stated or all_figures, 'all_figures': all_figures,
                   'similarity': round(max(best, 0.0), 3)}
    return _CACHE[key]


# ============================================================ DELIVERED FILES
def output_roots() -> list:
    roots = _os.environ.get('ATLAS_OUTPUT_ROOTS') or _os.pathsep.join(('/workspace', '/logs/artifacts'))
    return [r for r in roots.split(_os.pathsep) if r and _os.path.isdir(r)]


def delivered_files(exts=('.html', '.htm')) -> list:
    """Paths of delivered files with one of `exts`, largest first. The read-only evidence
    mount (data/) and scratch dirs are skipped."""
    key = ('files', tuple(exts))
    if key not in _CACHE:
        found = []
        for root in output_roots():
            for here, dirs, files in _os.walk(root):
                dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith('.')]
                for f in files:
                    if f.lower().endswith(tuple(exts)):
                        p = _pathlib.Path(here) / f
                        try:
                            found.append((p.stat().st_size, p))
                        except OSError:
                            continue
        _CACHE[key] = [p for _s, p in sorted(found, key=lambda t: -t[0])]
    return _CACHE[key]


def page_source() -> str:
    """Raw source of the delivered html (largest, if several). Raw, not rendered text,
    because runs keep their figures in JS arrays the tables are drawn from."""
    if 'page' not in _CACHE:
        files = delivered_files(('.html', '.htm'))
        _CACHE['page'] = files[0].read_text(errors='replace') if files else ''
    return _CACHE['page']


def page_numbers() -> set:
    src = page_source()
    return numbers_in(_re.sub(r'<[^>]+>', ' ', src), words=False) | numbers_in(src, words=False)


def table_rows(exts=('.csv', '.json')) -> list:
    """Every record in every delivered CSV / JSON, keys normalised like Channel A's _norm
    (casefold, alphanumerics), values as strings. JSON lists of dicts and dicts holding
    lists of dicts are flattened; anything else is ignored."""
    key = ('rows', tuple(exts))
    if key in _CACHE:
        return _CACHE[key]
    rows = []
    for p in delivered_files(exts):
        try:
            text = p.read_text(errors='replace')
        except OSError:
            continue
        if p.suffix.lower() == '.csv':
            try:
                for r in _csv.DictReader(text.splitlines()):
                    rows.append({norm(k): str(v).strip() for k, v in r.items() if k is not None})
            except _csv.Error:
                continue
        else:
            try:
                doc = _json.loads(text)
            except ValueError:
                continue
            stack = [doc]
            while stack:
                cur = stack.pop()
                if isinstance(cur, list):
                    stack.extend(cur)
                elif isinstance(cur, dict):
                    if cur and all(not isinstance(v, (dict, list)) for v in cur.values()):
                        rows.append({norm(k): str(v).strip() for k, v in cur.items()})
                    else:
                        stack.extend(cur.values())
    _CACHE[key] = rows
    return rows


def rows_with(*keys) -> list:
    """Delivered records carrying every one of `keys` (normalised)."""
    want = [norm(k) for k in keys]
    return [r for r in table_rows() if all(w in r for w in want)]


def near(value, target, tol) -> bool:
    v = as_float(value)
    return v is not None and _math.isfinite(v) and abs(v - target) <= tol + 1e-9


EXTERNAL_RE = _re.compile(r'''(?:src|href)\s*=\s*["']?\s*(?:https?:)?//|@import\b|\bfetch\s*\(|\burl\(\s*["']?https?://|\bimport\s*\(''', _re.I)
ABBREV_MONEY_RE = _re.compile(r'[$£]\s?\d+(?:\.\d+)?\s?(?:m|mn|bn|k)\b|\b\d+(?:\.\d+)?\s?(?:m|mn|bn|k)\b(?=[^<]*</text>)', _re.I)
