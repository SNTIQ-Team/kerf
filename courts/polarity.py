"""Relation polarity: a separate, rules-first stage after parsing (operating principle 3).

The parser writes only ``mentions`` / ``quotes`` / ``procedural_reference``
into ``relations``; those rows are never changed here.  This stage reads each
parser relation with its paragraph text and proposes, in the separate table
``relation_assessments``, a stronger reading when a German cue phrase in the
same clause supports it:

    norms      applies | interprets | holds_inapplicable | rejects_application_of
    decisions  follows | departs_from | overrules | distinguishes

Every proposal carries the cue, the rule id, the evidence span,
``derived_by: rules`` and ``legal_effect_verified: false``.  Negative
readings are first-class (polarity ``negative``); "supporting case law" views
must not present them as support.  Precision over recall: when a clause has
no cue, or a negation precedes a positive cue, nothing is proposed.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass

from .citations import procedural_clause

CLASSIFIER_VERSION = "courts-polarity-rules/1.3.3"
ASSESSMENTS = {
    "applies": "positive", "interprets": "neutral", "holds_inapplicable": "negative",
    "rejects_application_of": "negative", "follows": "positive", "departs_from": "negative",
    "overrules": "negative", "distinguishes": "negative",
    # A cue was found, but the rule did not reach 0.9 precision on the gold sample: no polarity claim.
    # assess() reports it (evaluation, conformance); the corpus does not store it while the schema is v3
    # (the assessment CHECK list is part of the v3 contract): the rule is disabled there.
    "mentions_with_cue": "neutral",
}
STORED_ASSESSMENTS = {a for a in ASSESSMENTS if a != "mentions_with_cue"}
# Procedural codes are "applied" only by courts of their own branch.  Elsewhere a
# citation is at most a mention (conformance: § 84 SGG in a Wehrdienst decision).
PROCEDURAL_CODES = {"de/bund/sgg": "sozial", "de/bund/vwgo": "verwaltung", "de/bund/fgo": "finanz",
                    "de/bund/arbgg": "arbeit", "de/bund/zpo": "ordentlich", "de/bund/stpo": "ordentlich",
                    "de/bund/famfg": "ordentlich", "de/bund/bverfgg": "verfassung", "de/bund/wdo_2002": "verwaltung",
                    "de/bund/wbo": "verwaltung"}
NEGATION_BEFORE = re.compile(r"\b(?:nicht|kein(?:e[nmrs]?)?|ohne)\s+(?:\w+\s+){0,2}$", re.I)


@dataclass(frozen=True)
class Rule:
    rule_id: str
    assessment: str
    target_kind: str          # norm | decision
    pattern: re.Pattern
    where: str                # clause | after | before  (relative to the citation)
    confidence: float
    own_court: bool | None = None   # decisions: only for own (True) / other (False) courts; None = any
    demoted: bool = False           # below 0.9 precision on the gold sample: mentions_with_cue, not stored


def _r(p: str) -> re.Pattern:
    return re.compile(p, re.I)


RULES = [
    # --- norms: negative (the norm precedes its cue, no other citation in between) -------------
    Rule("norm.keine_anwendung", "holds_inapplicable", "norm",
         _r(r"^[^§]{0,80}?(?:\bfinde[nt]\s+(?:\w+\s+){0,4}?)?\bkeine\s+Anwendung"), "after", 0.8),
    Rule("norm.nicht_anwendbar", "holds_inapplicable", "norm",
         _r(r"^[^§]{0,120}?\b(?:ist|sind|war|w[äa]re|w[äa]ren)\s+(?:[\w-]+\s+){0,5}?(?:nicht\s+|un)"
            r"(?:anwendbar|anzuwenden|einschl[äa]gig)\b"), "after", 0.8),
    Rule("norm.nicht_einschlaegig", "holds_inapplicable", "norm", _r(r"^[^§]{0,120}?\bnicht\s+einschl[äa]gig\b"),
         "after", 0.75),
    Rule("norm.bleibt_unangewendet", "holds_inapplicable", "norm", _r(r"^[^§]{0,120}?\bunangewendet\b"), "after", 0.75),
    # "hat § X fehlerhaft angewandt": the norm precedes the cue and the verb is "angewandt/angewendet"
    Rule("norm.zu_unrecht_angewandt", "rejects_application_of", "norm",
         _r(r"^[^§]{0,160}?\b(?:zu\s+Unrecht|rechtsfehlerhaft|fehlerhaft|unrichtig)\s+(?:[\w-]+\s+){0,3}?"
            r"(?:angewandt|angewendet)\b"), "after", 0.8),
    # --- norms: positive / neutral --------------------------------------------------------------
    Rule("norm.beruht_auf", "applies", "norm",
         _r(r"\b(?:beruht|beruhen|st[üu]tzt\s+sich|folgt|folgen|ergibt\s+sich|ergeben\s+sich)\s+(?:\w+\s+){0,2}?"
            r"(?:auf|aus)\s*$"), "before", 0.8),
    Rule("norm.unter_anwendung", "applies", "norm", _r(r"\bunter\s+Anwendung\s+(?:des|der|von)\s*$"), "before", 0.8,
         demoted=True),
    Rule("norm.rechtsgrundlage", "applies", "norm",
         _r(r"\b(?:Rechtsgrundlage|Anspruchsgrundlage|Erm[äa]chtigungsgrundlage)\s+(?:\w+\s+){0,5}?"
            r"(?:ist|sind|bildet|findet\s+sich\s+in)\s*$"), "before", 0.75),
    Rule("norm.auf_grundlage", "applies", "norm", _r(r"\bauf\s+(?:der\s+)?Grundlage\s+(?:des|der|von)\s*$"), "before",
         0.7, demoted=True),
    Rule("norm.gemaess_ist", "applies", "norm",
         _r(r"^\s*(?:ist|sind|hat|haben|wird|werden|war|waren|kann|k[öo]nnen|muss|m[üu]ssen)\b"), "after", 0.55),
    Rule("norm.auslegen", "interprets", "norm",
         _r(r"^[^§]{0,60}?\b(?:ist|sind)\s+(?:\w+\s+){0,3}?(?:dahin(?:gehend)?|so)\s+(?:\w+\s+){0,3}?"
            r"(?:auszulegen|zu\s+verstehen)"), "after", 0.75),
    # Fresh samples 2026-09-28: 0.73 (other actors' interpretations, "betrifft die Auslegung") - demoted.
    Rule("norm.auslegung_des", "interprets", "norm", _r(r"\bAuslegung\s+(?:des|der|von)\s*$"), "before", 0.7,
         demoted=True),
    Rule("norm.im_sinne_wenn", "interprets", "norm", _r(r"^[^.;§]{0,40}?,\s*wenn\b(?!\s+nicht\s+gar)"), "after", 0.6),
    # --- decisions ------------------------------------------------------------------------------
    Rule("decision.im_anschluss_an", "follows", "decision", _r(r"\bim\s+Anschluss\s+an\s*(?:\w+\s+){0,6}$"), "before",
         0.8),
    Rule("decision.in_uebereinstimmung", "follows", "decision",
         _r(r"\bin\s+(?:\w+\s+)?[ÜU]bereinstimmung\s+mit\s*(?:\w+\s+){0,6}$"), "before", 0.75),
    Rule("decision.staendige_rspr", "follows", "decision",
         _r(r"\bst[äa]ndige[rn]?\s+(?:\w+\s+){0,2}Rechtsprechung\b"), "clause", 0.6),
    Rule("decision.abweichend_von", "departs_from", "decision",
         _r(r"\babweichend\s+von\s*(?:\w+\s+){0,6}$"), "before", 0.8),
    # "Entgegen der Auffassung/Ansicht/Entscheidung des <court>": the court's own view against the cited one
    Rule("decision.entgegen", "departs_from", "decision",
         _r(r"\bentgegen\s+(?:der|dem|den)\s+(?:[\w-]+\s+){0,3}?(?:Auffassung|Ansicht|Ausf[üu]hrungen|Entscheidung|"
            r"Beschluss|Urteil|Urteilen|Rechtsprechung|Meinung|Beurteilung)\s+(?:des|der|vom)?\s*(?:\w+\s+){0,3}$"),
         "before", 0.7, demoted=True),  # fresh sample 2026-09-28: 0.59 (others acting "entgegen der Rechtsprechung")
    # Fresh sample 2026-09-28: 0/3 (parties invoking a decision "unter Hinweis auf" / "in Anlehnung an") - demoted.
    Rule("decision.anders_als_meint", "departs_from", "decision",
         _r(r"\banders\s+als\s+(?:der|die|das)\s+[^,;]{0,80}$"), "before", 0.7, demoted=True),
    Rule("decision.haelt_nicht_fest", "overrules", "decision",
         re.compile(r"\bh[äa]lt\s+an\s+.{0,240}?\bnicht\s+(?:mehr\s+)?fest\b|\bgibt\s+.{0,240}?"
                    r"Rechtsprechung\s+.{0,120}?\bauf\b", re.I | re.S), "clause", 0.75, own_court=True),
    Rule("decision.anders_als", "distinguishes", "decision",
         _r(r"\banders\s+als\s+(?:in\s+|im\s+)?(?:\w+\s+){0,6}$"), "before", 0.75),
    Rule("decision.nicht_vergleichbar", "distinguishes", "decision",
         _r(r"\b(?:nicht\s+vergleichbar|unterscheidet\s+sich|anders\s+gelagert|nicht\s+[üu]bertragbar)\b"),
         "clause", 0.7, demoted=True),
]
# Frames in which the court does not state its own holding about the cited target (checked in the clause
# text before the cue): questions left open, conditions, party submissions, negated consequences.
OPEN_FRAME = re.compile(r"\b(?:[Oo]b|[Ss]ollte[n]?|falls|sofern|unabh[äa]ngig\s+(?:von\s+der\s+Frage|davon)|"
                        r"dahinstehen|offenbleiben|offen\s+bleiben|offenlassen|Frage,?\s+ob)\b")
CLAIM_FRAME = re.compile(r"\b(?:ausgef[üu]hrt|vorgetragen|geltend\s+gemacht|ger[üu]gt|r[üu]gt|behauptet|meint|"
                         r"tr[äa]gt\s+vor|vertritt\s+die\s+(?:Auffassung|Ansicht)|beanstandet|"
                         r"aus\s+(?:seiner|ihrer|deren|dessen)\s+Sicht|legt\s+(?:\w+\s+){0,5}dar|darge(?:legt|tan)|"
                         r"zeigt\s+(?:[\w,]+\s+){0,8}?auf|Rechtsauffassung\s+(?:des|der)\s+(?:Kl[äa]ger|Beklagt|Antrag|"
                         r"Beigeladen|Beschwerdef)\w*|Einwand|Einwendung\w*)\b", re.I)
NEGATED_FRAME = re.compile(r"\b(?:f[üu]hr(?:t|en)\s+(?:.{0,80}?\s)?nicht\s+dazu|nicht\s+(?:\w+\s+){0,5}verpflichtet|"
                           r"nicht\s+gehindert|nicht\s+signifikant|nicht\s+ersichtlich)\b", re.I | re.S)
# For every type: "es ist nichts dafür ersichtlich, dass …" negates what follows.
NOTHING_EVIDENT = re.compile(r"\bnichts\s+(?:\w+\s+){0,2}ersichtlich\b|\bkeine\s+Anhaltspunkte\b", re.I)
# Konjunktiv I of reported submissions (lower case only: "Stelle", "Folge" are nouns).
KONJUNKTIV = re.compile(r"\b(?:knüpfe|weiche|verkenne|übersehe|verstoße|verletze|berücksichtige|stelle|lege|beziehe|"
                        r"kenne|treffe|lasse|halte|stütze|verneine|bejahe|nehme|ziehe|beruhe|betreffe|erfülle|genüge|"
                        r"enthalte|verlange|fordere|spreche|bezögen|verstieße|läge|hätte|wäre\s+(?:dem|der)\s+Kläger)\b")
PARTY = (r"(?:Kl[äa]ger\w*|Beklagte\w*|Antragsteller\w*|Antragsgegner\w*|Beigeladene\w*|Beteiligte\w*|"
         r"Beschwerdef[üu]hrer\w*|Rechtsmittelf[üu]hrer\w*|Beh[öo]rde\w*|Beschwerde)")
COURT_ACTOR = (r"(?:EuGH|EGMR|BVerfG|BGH|BSG|BVerwG|BFH|BAG|OVG|VGH|LSG|OLG|LAG|LG|VG|SG|"
               r"\w*[Gg]ericht\w*|\w*[Gg]erichtshof\w*|Senat|Kammer)")
# Norm cited as the basis of another norm's (in)applicability: "i.V.m. § X", "im Anwendungsbereich von § X",
# "unabhängig von § X", "aus Art. 6 GG, Art. 8 EMRK" (preceding citations of the same chain are skipped).
BASIS_BEFORE = re.compile(r"(?:\bi\.\s?V\.\s?m\.?|\biVm\.?|\bin\s+Verbindung\s+mit|\bunabh[äa]ngig\s+von|"
                          r"\bim\s+Anwendungsbereich\s+(?:von|des|der)|\baus)\s*"
                          r"(?:(?:§§?|Art\.?)\s*[^,;()§]{1,40}?(?:,|\bund\b|\bsowie\b)\s*)*$", re.I)
IS_STANDARD_BEFORE = re.compile(r"(?:\bi\.?\s?S\.?\s?(?:d\.|des|der|von|v\.)|\bim\s+Sinne\s+(?:des|der|von))\s*$", re.I)
VIEW_VERB = re.compile(r"\b(?:meint|meinen|annimmt|annehmen|ausf[üu]hrt|vertritt|vertreten|entschieden\s+hat|"
                       r"angenommen|erwogen|gehandhabt)\b", re.I)
FACT_COMPARISON = re.compile(r"^anders\s+als\s+(?:etwa\s+|noch\s+|insoweit\s+)?(?:in|im|bei|beim|zum|zur)\b|"
                             r"\b(?:Fall|F[äa]llen|Falles|Sachverhalt\w*|Konstellation\w*|zugrunde|entschiedene[nm]?|"
                             r"beurteilten|Zeitpunkt|Urteils?|Beschlusses|Beschluss|Entscheidung)\b", re.I)


HOLDS_KEY = re.compile(r"keine\s+Anwendung|nicht\s+(?:anwendbar|einschl[äa]gig|anzuwenden)|unanwendbar|unangewendet",
                       re.I)
HOLDS_VERB = re.compile(r"\b(?:finde[nt]|ist|sind|war|waren|w[äa]re|kann|k[öo]nnen|bleib\w*|keine\s+Anwendung|"
                        r"nicht\s+(?:anwendbar|einschl[äa]gig|anzuwenden)|unangewendet)\b", re.I)


def _strip_parens(text: str) -> str:
    return re.sub(r"\([^()]*\)", " ", text)


def _guarded_v13(rule: "Rule", text: str, start: int, end: int, cue_start: int, cue_end: int, lo: int,
                 before: str, prefix_text: str, clause: str, target_id: str | None = None) -> bool:
    """Guards added after the held-out sample of 2026-09-28 (rules 1.2.0 scored 0.24-0.90 there)."""
    cue = text[cue_start:cue_end]
    if NOTHING_EVIDENT.search(prefix_text):
        return True
    sentence_end = re.search(r"[.?!](?:\s|$)", text[max(end, cue_end):])
    if sentence_end and sentence_end.group().startswith("?"):
        return True  # a question (Vorlagefrage, "Stellt … dar, wenn …?") states no holding
    if rule.target_kind == "norm" and rule.assessment == "holds_inapplicable":
        between = text[end:cue_start] if cue_start > end else ""
        after_target = text[end:cue_end]
        if BASIS_BEFORE.search(before):
            return True  # the norm grounds another norm's inapplicability
        if re.match(r"\s*(?:[\w.]+\s+)?abweichende", text[end:end + 40]):
            return True  # "von § 300 Abs. 1 SGB VI abweichende Regelungen sind nicht einschlägig"
        head = _strip_parens(after_target)
        if ")" in head:  # the citation itself stood inside a parenthesis that closes here
            head = head[head.rindex(")") + 1:]
        verb = HOLDS_VERB.search(head)
        if verb and re.search(r",\s", head[:verb.start()]):
            return True  # the citation sits in an embedded clause closed before the verb ("…, wonach …, ist")
        if re.search(r"\)\s+(?:der|des|dem|den|die|das)\b", after_target):
            return True  # a parenthetical attached to another noun ("(bzw. Zustellung, § 56 …) der Entscheidung")
        if re.match(r"\s*(?:Wenn|Falls|Sofern)\b", text[lo:]):
            return True  # a conditional rule statement, not a holding
        if re.match(r"\s*(?:sein\s+)?(?:kann|k[öo]nnte|mag|d[üu]rfte)\b", text[cue_end:cue_end + 20]):
            return True  # "… nicht einschlägig sein kann"
        if re.search(r"\b(?:einschl[äa]gige[nmrs]?|anwendbare[nmrs]?|ma[ßs]gebliche[nmrs]?)\s*$", before):
            return True  # "neben dem hier … einschlägigen § 21 Abs. 4 Satz 3 PBefG": that norm applies
        if re.search(r"\b(?:k[öo]nnte|k[öo]nnten|d[üu]rfte|d[üu]rften|w[üu]rde|w[üu]rden|w[äa]re|w[äa]ren)\b", prefix_text):
            return True  # hypothetical ("Schließlich könnte … Art. 13 ARB 1/80 erst recht keine Anwendung finden, wenn …")
        if re.search(r"\b(?:im|in\s+dem|in\s+jenem)\s+(?:dort|damals|damalig\w*|dortig\w*)?\s*(?:entschiedenen\s+)?Fall\b",
                     prefix_text):
            return True  # "im dort entschiedenen Fall war § 275 … nicht einschlägig": another case's situation
        key = HOLDS_KEY.search(cue)
        head_key = _strip_parens(after_target[: (key.start() + (cue_start - end)) if key else len(after_target)])
        if ")" in head_key:
            head_key = head_key[head_key.rindex(")") + 1:]
        if key and re.search(r",\s", head_key):
            return True  # a comma between the citation and "keine Anwendung": another subject ("…, der aber keine …")
        if re.search(r"\b(?:d[üu]rfte|d[üu]rften|k[öo]nnte|k[öo]nnten|m[öo]gen|mag)\b", cue):
            return True  # tentative ("dürfte nicht einschlägig sein"), typically a reported or open view
        if re.search(r"\bso\s+(?:der|die|das)\s+" + COURT_ACTOR, clause):
            return True  # "– so der Bundesgerichtshof –": the cited court's view, reported
        if re.search(r"(?:\b(?:in|wegen|durch|aufgrund|zufolge|neben|von|gegen[üu]ber|au[ßs]er)\s+(?:der|des|dem)?\s*)$",
                     before) and not CONTENT_NOUN.search(re.sub(
                         r"\s*\b(?:in|wegen|durch|aufgrund|zufolge|neben|von|gegen[üu]ber|au[ßs]er)\s+(?:der|des|dem)?\s*$",
                         " ", before)):
            return True  # "wegen der fehlenden In-Bezugnahme in § 8a PBefG", "aufgrund der in § 50 VwVfG normierten …"
        if re.search(r",?\s*(?:und\s+)?(?:auf|f[üu]r|bei)\s+(?:den|die|das|dem|welche[nmrs]?)\s*$", before):
            return True  # a relative clause about someone else ("…, und auf den Art. 12 StatusVO keine Anwendung findet")
        if re.match(r"\s*(?:\w+\s+)?\w+(?:ten|tene[nm]?|ierte[nm]?|ene[nm])\s+[A-ZÄÖÜ]\w+", after_target):
            return True  # "§ 50 VwVfG normierten Regelung": the citation qualifies another noun
        if re.search(r"\b(?:Absatz|Abs\.|Satz|Nr\.|Buchst\.?)\s*\d", cue):
            return True  # another provision is the subject: "nach § 60 Abs. 8 … findet Absatz 1 keine Anwendung"
        closing = after_target.find(")")
        if closing >= 0 and "(" not in after_target[:closing] and not HOLDS_VERB.match(
                after_target[closing + 1:].lstrip()) and re.match(r"\s*[A-ZÄÖÜ]", after_target[closing + 1:]):
            return True  # "(§ 23 Abs. 1 KSchG) Geltungsbereichs …": the parenthesis qualifies another noun
    if rule.target_kind == "norm" and rule.assessment == "rejects_application_of":
        if IS_STANDARD_BEFORE.search(before):
            return True  # "das Recht iS des § 44 SGB X unrichtig angewandt": § 44 is the standard
        if re.search(r"\b(?:weder|nicht|kein\w*)\b", text[max(lo, start - 70):start] + " " + cue, re.I):
            return True
        if re.search(r"\b(?:wenn|falls|sofern|ob)\b", cue):
            return True  # a definition ("liegt nicht schon dann vor, wenn das Recht unrichtig angewendet wurde")
        if "„" in cue or "\"" in cue:
            return True  # the quoted tatbestand ("„Recht unrichtig angewandt“")
        if target_id and target_id.startswith("de/bund/sgb_10/para/44"):
            return True  # § 44 SGB X's own wording is "das Recht unrichtig angewandt": the standard, not the finding
        if re.match(r"\s*(?:worden\s+)?(?:w[äa]re|w[üu]rde|sei)\b", text[cue_end:cue_end + 25]):
            return True
    if rule.rule_id == "norm.beruht_auf" and (re.search(r"\b(?:nicht|kein\w*)\b", cue, re.I) or re.match(
            r"\s*(?:[\w.]+\s+){0,2}?(?:kein\w*|nicht)\b", text[end:end + 40], re.I)):
        return True  # "folgt nicht aus § 9a Abs. 2 WEG", "folgt aus § 42 HmbSG kein Recht auf …"
    if rule.rule_id == "norm.im_sinne_wenn" and re.sub(r",?\s*wenn\s*$", "", cue).strip(" ,") and not re.search(
            r"\b(?:ist|sind|liegt|liegen|vor|besteht|bestehen|gegeben|begr[üu]ndet|anzunehmen|auszugehen|erf[üu]llt|"
            r"gilt|gelten|dann|nur|verdichtet|qualifizieren|verstehen|Frage|Rechtssache|bedeutsam|anzusehen|"
            r"handelt|hat|haben)\b", text[end:cue_end]):
        return True  # "X im Sinne des § Y drohen/veräußern, wenn …": the "wenn" belongs to another rule, no definition
    if rule.target_kind == "norm" and procedural_clause(text, start):
        return True  # the court's cost/enforceability/value ruling (procedural_reference since parser 1.6.0)
    if rule.rule_id == "norm.auslegung_des" and re.search(
            r"\b(?:seine|ihre)\s+Auslegung|\bim\s+(?:angefochtenen?|angegriffenen?)\s+(?:Urteil|Beschluss)|"
            r"\bauf\s+die\s+Auslegung\b", clause, re.I):
        return True  # another court's interpretation
    if rule.rule_id == "decision.staendige_rspr":
        if cue_start > end and re.search(r"\bvgl\.", text[end:cue_start], re.I):
            return True  # the cue belongs to a later citation group
        if re.search(r"\b(?:teilweise|verschiedentlich|vereinzelt|zum\s+Teil)\b", clause, re.I):
            return True  # a report of divergent positions
    if rule.rule_id == "decision.im_anschluss_an":
        opened = text.rfind("(", lo, cue_start)
        in_parens = opened >= 0 and text.find(")", opened, cue_start) < 0
        at_start = not text[lo:cue_start].strip() or re.fullmatch(r"\s*(?:\w+\s+){0,2}", text[lo:cue_start])
        if not (in_parens or at_start or re.search(r"\b(?:Senat|Kammer|Gericht|wir)\b", text[lo:cue_start])):
            return True  # someone else follows ("die Beschlüsse … setzten sich im Anschluss an … zusammen")
        if in_parens and re.search(r"\b(?:Urteil|Beschluss|Urt\.|Beschl\.)\s+v(?:om|\.)", text[opened:cue_start]):
            return True  # "(vgl. BVerwG, Beschluss vom … im Anschluss an EuGH …)": the cited court follows
    if rule.rule_id == "decision.anders_als":
        if not (FACT_COMPARISON.search(cue.strip()) or re.search(r"\b(?:Fall|Sachverhalt)\b", prefix_text)):
            return True  # "anders als das LSG" compares views, not cases
        if VIEW_VERB.search(text[cue_start:min(len(text), end + 60)]):
            return True  # "anders als noch vom Senat … angenommen": a change of view, not a distinction
    if rule.rule_id == "decision.anders_als_meint":
        lead = text[cue_start:start]
        if re.search(r"\b(?:unter|mit)\s+Verweis\s+auf|\bunter\s+Berufung\s+auf|\bgest[üu]tzt\s+auf", lead, re.I):
            return True  # a party invoking the decision
        if VIEW_VERB.search(lead) and not re.search(r"\bmit\s+(?:dem|der)\s+" + COURT_ACTOR, lead):
            return True  # "anders als der Beklagte meint – … (OVG …)": the citation supports the court
        if re.search(r"\bf[üu]r\s+das\s+\w+recht\b", text[cue_start:end + 80]):
            return True  # a view limited to another field of law: no disagreement
    if rule.rule_id == "decision.abweichend_von":
        if re.search(r"\b(?:Fall|F[äa]llen|Sachverhalt\w*|Konstellation\w*|Fallgestaltung)\b", text[cue_start:end + 40]):
            return True  # "Abweichend von dem … zu entscheidenden Fall hat die Klägerin …": facts, not a departure
        if KONJUNKTIV.search(clause):
            return True  # reported speech ("… knüpfe das LSG nicht an …")
    if rule.rule_id == "decision.entgegen":
        if re.search(r"\b" + PARTY, cue) and not re.search(r"\bund\s+(?:des|der|dem)\b", cue):
            return True  # "entgegen der Auffassung des Klägers auch aus EuGH …"
        if re.search(r"(?:\b(?:die|der|das)\s+" + PARTY + r"|\b(?:hat|haben|hatte)\s+(?:der|die|das)\s+" + COURT_ACTOR
                     + r")\s*[-–]?\s*$", text[lo:cue_start]):
            return True  # someone else acts against the cited view (the authority; "hat der EuGH – entgegen …")
    return False
HYPOTHETICAL = re.compile(r"\b(?:f[äa]nde|f[äa]nden|w[äa]re|w[äa]ren|w[üu]rde|w[üu]rden|k[äa]me)\b", re.I)
# "X nach/gemäß § N" where X names the norm's own content: the norm is the object, not a source.
CONTENT_NOUN = re.compile(r"(?:vorbehalt|regelung|privilegierung|voraussetzung(?:en)?|befugnis|anspruch|frist|"
                          r"fristbestimmung|bestimmung|verbot|pflicht|ausnahme|vorschrift|norm|tatbestand|"
                          r"rechtsfolge|erm[äa]chtigung|zuschlag|leistung|verg[üu]nstigung|einschr[äa]nkung|"
                          r"bestimmungen|regelungen|beschr[äa]nkung(?:en)?|grundsatz|fiktion|auslegungsregel|"
                          r"schulpflicht\s+in|verweisung)\s+$", re.I)
LOCAL_CLAUSE_SPLIT = re.compile(r",\s*(?:denn|doch|aber|jedoch|sondern)\s|;\s")
# The cited norm is the source of an exclusion ("nach § 5 Abs. 3 BetrVG … keine Anwendung"), not its object.
SOURCE_BEFORE = re.compile(r"(?:\b(?:nach|gem[äa][ßs]s?|gem\.|aufgrund|kraft|wegen)|"
                           r"\b(?:im\s+Hinblick\s+auf|mit\s+Blick\s+auf|wegen|aufgrund)\s+(?:\S+\s+){0,4}(?:der|des))\s*$",
                           re.I)
SUBORDINATE_BREAK = re.compile(r",\s*(?:wobei|w[äa]hrend|obwohl|sodass|so\s+dass|weil|da|dass|wonach|zumal)\b")
OTHER_ACTOR_READING = re.compile(r"\bgeh(?:t|en)\s+.{0,80}?\bvon\s+(?:einer|der)\s+.{0,40}?Auslegung\b", re.I | re.S)
PARTY_VIEW_AFTER = re.compile(r"^.{0,80}?\bgest[üu]tzten\s+(?:Auffassung|Ansicht)\b", re.S)
PRESENT_CASE = re.compile(r"\b(?:vorliegend\w*|hiesig\w*|hier\b|derartig\w*|Streitfall|der\s+Fall\b)", re.I)
# Reported speech (Konjunktiv I) states a party's or a lower court's view, not the court's own holding.
REPORTED_SPEECH = re.compile(r"\b(?:sei|seien|habe|hätten|finde|gelte|komme|fehle|bestehe|liege|werde|müsse|könne|"
                             r"dürfe|stehe|folge|ergebe)\b", re.I)
# Sections in which the court reports facts and submissions rather than deciding.
NO_ASSESSMENT_SECTIONS = {"tatbestand", "titel", "orientierungssatz"}
CITATION_TOKEN = re.compile(r"§|\bArt\.\s*\d")



def inside_quotation(text: str, position: int) -> bool:
    """True if position lies inside „…“ / "…" quoted material (a quoted clause is not the court's holding)."""
    opened = text.rfind("„", 0, position)
    if opened >= 0 and text.find("“", opened, position) < 0:
        return True
    return text.count('"', 0, position) % 2 == 1


# Clause boundaries: ';', ':' or a full stop that ends a sentence (not "Abs.", "Nr.", "vgl.", dates).
_ABBREV = r"(?:Abs|Nr|Nrn|Rn|Rdnr|Randnr|Art|Artt|vgl|Vgl|S|Buchst|lit|Urt|Beschl|Az|ff|f|u|a|m|w|N|v|Bd|Aufl|Anm|ca|bzw|z|B|d|h|i|V|Alt|Hs|Var|UAbs|Satz|Ziff|Kap|Tz|Fn|Einl|Vorb|zit|aaO|jew|insb|ggf|usw|sog|Dr|Prof)"
_BOUNDARY = re.compile(r";|:\s|(?<!\b" + _ABBREV[3:-1].replace("|", r")(?<!\b") + r")(?<![\d\s])\.\s+(?=[A-ZÄÖÜ„\"(])")


def clause_bounds(text: str, start: int, end: int, *, limit: int = 400) -> tuple[int, int]:
    left = max(0, start - limit)
    right = min(len(text), end + limit)
    lo = left
    for m in _BOUNDARY.finditer(text, left, start):
        lo = m.end()
    hi = right
    m = _BOUNDARY.search(text, end, right)
    if m:
        hi = m.start()
    return lo, hi


@dataclass
class Proposal:
    assessment: str
    rule: Rule
    cue_start: int
    cue_end: int
    clause: tuple[int, int]


def assess(text: str, span: tuple[int, int], *, target_kind: str, target_id: str | None,
           own_court_id: str | None, jurisdiction: str | None, detail: dict | None = None) -> list[Proposal]:
    """Rule proposals for one parser relation (a norm or decision citation at ``span``)."""
    start, end = span
    lo, hi = clause_bounds(text, start, end)
    before, after, clause = text[lo:start], text[end:hi], text[lo:hi]
    detail = detail or {}
    proposals: list[Proposal] = []
    if inside_quotation(text, start) or REPORTED_SPEECH.search(clause):
        return proposals
    if detail.get("procedural_basis"):
        return proposals  # cost/enforceability/value/finality provisions: procedural_reference, never applies
    if target_kind == "norm" and not target_id:
        return proposals  # an unresolved citation (e.g. a commentary reference "SGB X, § 44 RdNr 38") is not assessed
    other_citations = len(CITATION_TOKEN.findall(before)) + len(CITATION_TOKEN.findall(after))
    for rule in RULES:
        if rule.target_kind != target_kind:
            continue
        if rule.own_court is not None and target_kind == "decision":
            is_own = bool(own_court_id and detail.get("court_id") == own_court_id)
            if is_own != rule.own_court:
                continue
        if rule.where == "before":
            m = rule.pattern.search(before)
            offset = lo
        elif rule.where == "after":
            m = rule.pattern.search(after)
            offset = end
        else:
            if target_kind == "norm" and other_citations:
                continue  # a clause-wide cue cannot be attributed to one of several norms
            m = rule.pattern.search(clause)
            offset = lo
        if not m:
            continue
        cue_start, cue_end = offset + m.start(), offset + m.end()
        polarity = ASSESSMENTS[rule.assessment]
        if polarity == "positive" and NEGATION_BEFORE.search(text[max(lo, cue_start - 40):cue_start]):
            continue  # "nicht unter Anwendung des § X"
        if rule.rule_id == "norm.gemaess_ist" and not re.fullmatch(r"\s*(?:\(?\w{1,3}[.)]\s*)?(?:Gem[äa][ßs]s?|Nach)\s*",
                                                                    before, re.I):
            continue  # only a clause opened by "Gemäß/Nach § X ist/kann/muss ..."; not "Einschränkungen … nach § X sind"
        if rule.rule_id == "norm.im_sinne_wenn" and not re.search(r"\bim\s+Sinne\s+(?:des|der|von)\s*$", before, re.I):
            continue
        prefix_text = LOCAL_CLAUSE_SPLIT.split(text[lo:min(start, cue_start)])[-1]
        prefix_text = re.sub(r"\bwie\s+(?:\w+\s+){0,2}(?:ausgef[üu]hrt|dargelegt|dargestellt)\b", " ", prefix_text)
        if rule.assessment != "follows" and (OPEN_FRAME.search(prefix_text) or CLAIM_FRAME.search(prefix_text)):
            continue
        if polarity == "negative" and rule.target_kind == "norm" and re.search(r"\bwenn\b", prefix_text):
            continue  # "Ein Verstoß liegt erst vor, wenn … fehlerhaft angewandt hat"
        if polarity == "negative" and NEGATED_FRAME.search(LOCAL_CLAUSE_SPLIT.split(text[lo:cue_end])[-1]):
            continue
        if rule.assessment in ("holds_inapplicable", "rejects_application_of"):
            source = SOURCE_BEFORE.search(before)
            if source and not CONTENT_NOUN.search(before[:source.start()]):
                continue
            between = text[end:cue_end]
            if SUBORDINATE_BREAK.search(between) or re.search(r"\bund\b", text[end:max(end, cue_end - 20)]) \
                    or HYPOTHETICAL.search(between):
                continue
        if rule.assessment == "interprets" and OTHER_ACTOR_READING.search(clause):
            continue
        if rule.rule_id == "decision.entgegen" and PARTY_VIEW_AFTER.search(after):
            continue  # "entgegen der auf das Urteil des VG … gestützten Auffassung der Antragstellerin"
        if rule.rule_id == "decision.abweichend_von" and re.search(r"Streitwert", clause):
            continue
        if rule.assessment == "overrules" and end < cue_start:
            continue  # the overruled decision (its Az) is cited after "hält an …" / "gibt …", not before
        if rule.rule_id == "decision.anders_als" and re.search(r"\b(?:meint|annimmt|ausf[üu]hrt|vertritt|entschieden\s+hat)\b",
                                                                 text[cue_start:hi]):
            continue  # "Anders als der BayVGH meint": disagreement (decision.anders_als_meint)
        if rule.rule_id == "decision.anders_als_meint" and not re.search(
                r"\b(?:meint|annimmt|ausf[üu]hrt|vertritt|entschieden\s+hat)\b", text[cue_start:hi]):
            continue
        if rule.rule_id == "decision.nicht_vergleichbar" and not PRESENT_CASE.search(clause):
            continue
        if _guarded_v13(rule, text, start, end, cue_start, cue_end, lo, before, prefix_text, clause, target_id):
            continue
        assessment = "mentions_with_cue" if rule.demoted else rule.assessment
        proposals.append(Proposal(assessment, rule, cue_start, cue_end, (lo, hi)))
    if target_kind == "norm" and target_id:
        prefix = "/".join(target_id.split("/")[:3])
        home = PROCEDURAL_CODES.get(prefix)
        if home and jurisdiction and home != jurisdiction and not detail.get("in_conjunction_with"):
            # A procedural code of another branch is not applied here: mentions only.
            proposals = [p for p in proposals if p.assessment not in ("applies", "interprets")]
    # One proposal per assessment (the most confident rule).
    best: dict[str, Proposal] = {}
    for p in proposals:
        if p.assessment not in best or p.rule.confidence > best[p.assessment].rule.confidence:
            best[p.assessment] = p
    return list(best.values())


SCHEMA = """
CREATE TABLE IF NOT EXISTS relation_assessments(
  id INTEGER PRIMARY KEY,
  relation_id INTEGER,
  decision_id TEXT NOT NULL,
  paragraph_id TEXT,
  target_kind TEXT NOT NULL CHECK(target_kind IN ('norm','decision')),
  target_id TEXT,
  span_start INTEGER,
  span_end INTEGER,
  assessment TEXT NOT NULL CHECK(assessment IN ('applies','interprets','holds_inapplicable',
    'rejects_application_of','follows','departs_from','overrules','distinguishes')),
  polarity TEXT NOT NULL CHECK(polarity IN ('positive','negative','neutral')),
  cue TEXT NOT NULL,
  cue_start INTEGER,
  cue_end INTEGER,
  evidence_text TEXT NOT NULL,
  rule_id TEXT NOT NULL,
  derived_by TEXT NOT NULL CHECK(derived_by IN ('rules','model','user')),
  legal_effect_verified INTEGER NOT NULL DEFAULT 0 CHECK(legal_effect_verified IN (0,1)),
  confidence REAL NOT NULL,
  classifier_version TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'proposed' CHECK(status IN ('proposed','confirmed','rejected')),
  created_at TEXT NOT NULL,
  CHECK(derived_by = 'user' OR legal_effect_verified = 0)
);
CREATE INDEX IF NOT EXISTS relation_assessments_decision ON relation_assessments(decision_id);
CREATE INDEX IF NOT EXISTS relation_assessments_target ON relation_assessments(target_id, assessment);
"""


def assess_decision(conn: sqlite3.Connection, decision_id: str) -> int:
    """(Re)compute rule proposals for one decision; user/model rows are kept."""
    decision = conn.execute("SELECT court_slug AS court_id, jurisdiction FROM decisions WHERE id=?",
                            (decision_id,)).fetchone()
    if decision is None:
        return 0
    rows = conn.execute(f"""SELECT r.id, r.paragraph_id, r.target_kind, r.target_id, r.span_start, r.span_end,
                                  r.detail, p.text
                           FROM relations r JOIN paragraphs p ON p.id = r.paragraph_id
                           WHERE r.decision_id = ? AND r.origin = 'decision_text' AND r.span_start IS NOT NULL
                             AND r.derived_by = 'parser' AND p.authorship = 'court'
                             AND r.relation IN ('mentions','quotes')
                             AND p.section NOT IN ({','.join('?' * len(NO_ASSESSMENT_SECTIONS))})""",
                        (decision_id, *sorted(NO_ASSESSMENT_SECTIONS))).fetchall()
    out = []
    for row in rows:
        for proposal in assess(row["text"], (row["span_start"], row["span_end"]), target_kind=row["target_kind"],
                               target_id=row["target_id"], own_court_id=decision["court_id"],
                               jurisdiction=decision["jurisdiction"], detail=json.loads(row["detail"] or "{}")):
            if proposal.assessment not in STORED_ASSESSMENTS:
                continue  # demoted rule: disabled in the corpus (see ASSESSMENTS)
            lo, hi = proposal.clause
            out.append((row["id"], decision_id, row["paragraph_id"], row["target_kind"], row["target_id"],
                        row["span_start"], row["span_end"], proposal.assessment, ASSESSMENTS[proposal.assessment],
                        row["text"][proposal.cue_start:proposal.cue_end], proposal.cue_start, proposal.cue_end,
                        " ".join(row["text"][lo:hi].split()), proposal.rule.rule_id, proposal.rule.confidence,
                        CLASSIFIER_VERSION))
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM relation_assessments WHERE decision_id=? AND derived_by='rules'", (decision_id,))
        conn.executemany("""INSERT INTO relation_assessments(relation_id, decision_id, paragraph_id, target_kind,
                              target_id, span_start, span_end, assessment, polarity, cue, cue_start, cue_end,
                              evidence_text, rule_id, derived_by, legal_effect_verified, confidence,
                              classifier_version, created_at)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'rules',0,?,?,
                                   strftime('%Y-%m-%dT%H:%M:%S+00:00','now'))""", out)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return len(out)
