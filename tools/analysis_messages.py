"""Multilingual message table for the procedure analysis.

`procedure_analysis.py` used to emit German prose as data, and the frontend
rendered it verbatim in every language — so Russian and Ukrainian readers, who
are most of the audience, got German sentences in the most authoritative part
of the tracked-procedure card.

A message is emitted as an id plus its parameters plus the rendered text in
every UI language.  The flat German field stays alongside it, so any string
not yet converted keeps behaving exactly as before: the frontend prefers the
localized text and falls back to German.

Templates use str.format placeholders.  Keep them few and inert (dates,
identifiers, citations) — never assemble a sentence out of translated
fragments, because word order differs across these four languages.
"""
from __future__ import annotations

LANGS = ("de", "en", "ru", "ua")

MESSAGES: dict[str, dict[str, str]] = {
    # ---- shared source observation -------------------------------------
    "fact.status.observed": {
        "de": "Amtlicher Status: {status}; Stadium: {stage}.",
        "en": "Official status: {status}; stage: {stage}.",
        "ru": "Официальный статус: {status}; стадия: {stage}.",
        "ua": "Офіційний статус: {status}; стадія: {stage}.",
    },
    "fact.status.stale": {
        "de": "Die jüngste EUR-Lex-Abfrage ist fehlgeschlagen; angezeigt wird "
              "die letzte bestätigte amtliche Beobachtung.",
        "en": "The most recent EUR-Lex query failed; the last confirmed "
              "official observation is shown.",
        "ru": "Последний запрос к EUR-Lex не удался; показано последнее "
              "подтверждённое официальное наблюдение.",
        "ua": "Останній запит до EUR-Lex не вдався; показано останнє "
              "підтверджене офіційне спостереження.",
    },
    "fact.status.missing": {
        "de": "Der Eintrag fehlt im jüngsten amtlichen Snapshot; der vorherige "
              "Status wird nicht als aktuelle Beobachtung ausgegeben.",
        "en": "The entry is absent from the latest official snapshot; the "
              "previous status is not reported as a current observation.",
        "ru": "Записи нет в последнем официальном снимке; предыдущий статус не "
              "выдаётся как текущее наблюдение.",
        "ua": "Запису немає в останньому офіційному знімку; попередній статус "
              "не подається як поточне спостереження.",
    },
    # ---- publication and legal effect ----------------------------------
    "fact.oj.published": {
        "de": "Im Amtsblatt der EU veröffentlicht: {celex} ({citation}). "
              "Quelle: {source}.",
        "en": "Published in the Official Journal of the EU: {celex} "
              "({citation}). Source: {source}.",
        "ru": "Опубликовано в Официальном журнале ЕС: {celex} ({citation}). "
              "Источник: {source}.",
        "ua": "Опубліковано в Офіційному журналі ЄС: {celex} ({citation}). "
              "Джерело: {source}.",
    },
    "fact.inforce": {
        "de": "In Kraft seit {since} ({celex}). Quelle: {source}.",
        "en": "In force since {since} ({celex}). Source: {source}.",
        "ru": "Действует с {since} ({celex}). Источник: {source}.",
        "ua": "Чинний з {since} ({celex}). Джерело: {source}.",
    },
    "fact.inforce.staggered": {
        "de": "In Kraft seit {since} ({celex}). Weitere Geltungsdaten für "
              "gestaffelte Bestimmungen: {dates}. Quelle: {source}.",
        "en": "In force since {since} ({celex}). Further dates apply to "
              "staggered provisions: {dates}. Source: {source}.",
        "ru": "Действует с {since} ({celex}). Для отдельных положений "
              "предусмотрены иные даты: {dates}. Источник: {source}.",
        "ua": "Чинний з {since} ({celex}). Для окремих положень передбачені "
              "інші дати: {dates}. Джерело: {source}.",
    },
    # ---- source checks --------------------------------------------------
    "check.sourceAvailable.label": {
        "de": "Amtliche Quelle verfügbar",
        "en": "Official source available",
        "ru": "Официальный источник доступен",
        "ua": "Офіційне джерело доступне",
    },
    "check.sourceAvailable.missing": {
        "de": "Der beobachtete Vorgang fehlt derzeit in der amtlichen Quelle.",
        "en": "The monitored procedure is currently absent from the official "
              "source.",
        "ru": "Отслеживаемая процедура сейчас отсутствует в официальном "
              "источнике.",
        "ua": "Відстежувана процедура наразі відсутня в офіційному джерелі.",
    },
    "check.sourceAvailable.stale": {
        "de": "Die letzte bestätigte Beobachtung wird mit einer Stale-Warnung "
              "weitergeführt.",
        "en": "The last confirmed observation is carried forward with a "
              "staleness warning.",
        "ru": "Последнее подтверждённое наблюдение используется дальше с "
              "пометкой об устаревании.",
        "ua": "Останнє підтверджене спостереження використовується далі з "
              "позначкою про застарілість.",
    },
    "check.sourceAvailable.ok": {
        "de": "Status und Stadium stammen aus dem jüngsten amtlichen Snapshot.",
        "en": "Status and stage come from the latest official snapshot.",
        "ru": "Статус и стадия взяты из последнего официального снимка.",
        "ua": "Статус і стадія взяті з останнього офіційного знімка.",
    },
    "check.scopeSource.label": {
        "de": "Quelle für den beschriebenen Regelungsumfang",
        "en": "Source for the described scope",
        "ru": "Источник описанного объёма регулирования",
        "ua": "Джерело описаного обсягу регулювання",
    },
    "check.scopeSource.linked": {
        "de": "Der beschriebene Entwurfsumfang ist mit einer amtlichen Vorlage "
              "verknüpft.",
        "en": "The described draft scope is linked to an official document.",
        "ru": "Описанный объём проекта связан с официальным документом.",
        "ua": "Описаний обсяг проєкту пов'язаний з офіційним документом.",
    },
    "check.scopeSource.missing": {
        "de": "Für die Inhaltsbeschreibung ist noch keine gesonderte amtliche "
              "Vorlage verknüpft.",
        "en": "No separate official document is linked for the content "
              "description yet.",
        "ru": "Для описания содержания ещё не привязан отдельный официальный "
              "документ.",
        "ua": "Для опису змісту ще не прив'язано окремий офіційний документ.",
    },
    # ---- EU procedure ---------------------------------------------------
    "check.ojPublication.label": {
        "de": "Veröffentlichung im Amtsblatt",
        "en": "Publication in the Official Journal",
        "ru": "Публикация в Официальном журнале",
        "ua": "Публікація в Офіційному журналі",
    },
    "check.ojPublication.pending": {
        "de": "Die Fundstelle und der finale CELEX müssen erfasst werden.",
        "en": "The citation and the final CELEX still have to be recorded.",
        "ru": "Ссылку и итоговый CELEX ещё предстоит зафиксировать.",
        "ua": "Посилання та підсумковий CELEX ще належить зафіксувати.",
    },
    "inference.published.pendingReview": {
        "de": "Der Rechtsakt ist veröffentlicht; offen ist die inhaltliche "
              "Gegenprüfung des endgültigen Artikels 2.",
        "en": "The act is published; what remains open is the substantive "
              "comparison of the final Article 2.",
        "ru": "Акт опубликован; остаётся сверка содержания итоговой статьи 2.",
        "ua": "Акт опубліковано; лишається звірка змісту підсумкової статті 2.",
    },
    "milestone.finalTextReview.label": {
        "de": "Endfassung gegenprüfen",
        "en": "Cross-check the final text",
        "ru": "Сверить итоговый текст",
        "ua": "Звірити підсумковий текст",
    },
    "milestone.finalTextReview.detail": {
        "de": "Artikel 2 des veröffentlichten Rechtsakts wird mit "
              "{proposal} verglichen.",
        "en": "Article 2 of the published act is compared with {proposal}.",
        "ru": "Статья 2 опубликованного акта сверяется с {proposal}.",
        "ua": "Статтю 2 опублікованого акта звіряють із {proposal}.",
    },
    "inference.stillOpen": {
        "de": "EUR-Lex weist noch keinen angenommenen finalen Rechtsakt mit "
              "CELEX aus.",
        "en": "EUR-Lex does not yet show an adopted final act with a CELEX "
              "number.",
        "ru": "EUR-Lex пока не показывает принятый итоговый акт с номером "
              "CELEX.",
        "ua": "EUR-Lex поки не показує ухвалений підсумковий акт із номером "
              "CELEX.",
    },
    "factor.noOjCitation": {
        "de": "Ohne Amtsblattfundstelle ist weder Veröffentlichung noch "
              "Inkrafttreten belegt.",
        "en": "Without an Official Journal citation, neither publication nor "
              "entry into force is evidenced.",
        "ru": "Без ссылки на Официальный журнал не подтверждены ни "
              "публикация, ни вступление в силу.",
        "ua": "Без посилання на Офіційний журнал не підтверджені ні "
              "публікація, ні набрання чинності.",
    },
    "inference.monitoringComplete": {
        "de": "Überwachung abgeschlossen",
        "en": "Monitoring complete",
        "ru": "Наблюдение завершено",
        "ua": "Спостереження завершено",
    },
    "factor.archived": {
        "de": "Keine weitere häufige Abfrage; der validierte Abschluss bleibt "
              "archiviert.",
        "en": "No further frequent polling; the validated conclusion stays "
              "archived.",
        "ru": "Частые запросы прекращены; подтверждённое завершение остаётся "
              "в архиве.",
        "ua": "Часті запити припинено; підтверджене завершення лишається в "
              "архіві.",
    },
    "check.eurlexQueryFailed.label": {
        "de": "EUR-Lex-Abfrage vorübergehend fehlgeschlagen",
        "en": "EUR-Lex query temporarily failed",
        "ru": "Запрос к EUR-Lex временно не удался",
        "ua": "Запит до EUR-Lex тимчасово не вдався",
    },
    "check.eurlexQueryFailed.detail": {
        "de": "Die Prognose nutzt die letzte bestätigte amtliche Beobachtung "
              "und wird nach einer erfolgreichen Abfrage neu bewertet.",
        "en": "The forecast uses the last confirmed official observation and "
              "is re-evaluated after a successful query.",
        "ru": "Прогноз опирается на последнее подтверждённое официальное "
              "наблюдение и будет пересчитан после успешного запроса.",
        "ua": "Прогноз спирається на останнє підтверджене офіційне "
              "спостереження й буде перерахований після успішного запиту.",
    },
    "factor.staleObservation": {
        "de": "Die aktuelle EUR-Lex-Beobachtung ist als veraltet markiert.",
        "en": "The current EUR-Lex observation is flagged as stale.",
        "ru": "Текущее наблюдение EUR-Lex помечено как устаревшее.",
        "ua": "Поточне спостереження EUR-Lex позначене як застаріле.",
    },
}


def message(key: str, **params: object) -> dict:
    """Render one message in every UI language.

    Returns the id and parameters alongside the rendered text so a consumer
    can re-translate later without re-deriving the facts.
    """
    templates = MESSAGES[key]
    return {
        "key": key,
        "params": {name: str(value) for name, value in params.items()},
        "text": {lang: templates[lang].format(**params) for lang in LANGS},
    }


def german(value: object) -> str:
    """The flat German string for a message object or a plain string.

    Keeps the existing API fields byte-identical for consumers that have not
    been taught about the localized variant.
    """
    if isinstance(value, dict) and "text" in value:
        return str(value["text"]["de"])
    return str(value)


def localized(value: object) -> dict | None:
    """The per-language texts, or None for a not-yet-converted plain string."""
    if isinstance(value, dict) and "text" in value:
        return dict(value["text"])
    return None
