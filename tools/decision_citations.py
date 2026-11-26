"""Literal court-qualified citations in retained passages."""
import re


def key(az):
    return re.sub(r'\s+', '', az).replace('‑', '-').replace('–', '-').casefold()


def citation_matcher(decisions):
    """Compile the selected release's unambiguous docket and court patterns once."""
    candidates = {}
    for target in decisions:
        if target.get('az') and target.get('court_short'):
            candidates.setdefault(key(target['az']), []).append(target)
    targets = []
    for group in candidates.values():
        if len(group) != 1:
            continue
        target = group[0]
        parts = re.split(r'(\s+|[-‑–])', target['az'])
        pattern = ''.join(r'\s+' if p.isspace() else r'[-‑–]' if p in {'-', '‑', '–'} else re.escape(p) for p in parts)
        docket = re.compile(r'(?<!\w)' + pattern + r'(?!\w)')
        aliases = [re.escape(a) for a in (target['court_short'], target.get('court')) if a]
        court = re.compile(r'(?<!\w)(?:' + '|'.join(aliases) + r')(?!\w)')
        # The longest literal docket fragment must be present for the regex to match.
        literal = max((p for p in parts if p and not p.isspace() and p not in {'-', '‑', '–'}), key=len, default='')
        targets.append((target['id'], docket, court, literal))

    def match_passage(passage):
        text, citations = passage['text'], []
        for identity, docket, court, literal in targets:
            if identity == passage['decision_id'] or literal not in text:
                continue
            for match in docket.finditer(text):
                if not court.search(text[max(0, match.start() - 110):match.start()]):
                    continue
                citations.append({'relation': 'decision_citation', 'source_id': passage['decision_id'], 'target_id': identity,
                    'passage_id': passage['passage_id'], 'source_role': passage['source_role'], 'text_sha256': passage['text_sha256'],
                    'start': match.start(), 'end': match.end(), 'quote': match.group(), 'offset_unit': 'unicode_codepoints',
                    'treatment': 'unclassified', 'binding_basis': 'exact_docket_and_nearby_court_in_retained_text'})
        return sorted(citations, key=lambda c: (c['start'], c['target_id']))
    return match_passage


def citations_for(passage, decisions):
    return citation_matcher(decisions)(passage)
