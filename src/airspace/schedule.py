"""
Activation of airspace that is not always active. The OpenAir file has no schedules,
so events are classed by what is known about the type of airspace:

- intermittent areas (temporary reserved/segregated areas EHTRA/EHTSA and areas marked
  <notam>) are only active when booked by NOTAM / the airspace use plan: an event there
  is "unverified" — check the NOTAMs of that day before calling it a violation;
- restricted areas without an H24 tag are activated by NOTAM (AIP ENR 5.1): "unverified";
- control zones that can be crossed with a clearance on request (CTR Deelen via Dutch Mil):
  always "possible" — the pilot may have had permission;
- military TMAs/CTRs are normally active on weekdays only: on weekends and Dutch public
  holidays an event there is "possible" (probably not active); military CTRs with known
  opening hours (MIL_CTR_HOURS) are also "possible" outside those hours;
- everything else is always active: an event is a "violation";
- time inside a gliding sector is "exempt";
- airspace with a published time-dependent classification (TIME_RULES) only counts while it
  is class D; the rest of the time it is class E, where VFR gliders need no clearance.
"""

import re
from datetime import date, datetime, timedelta, timezone

import numpy as np

# Military TMAs/CTAs and military aerodrome control zones (weekday operations)
PART_TIME_DEFAULT = r'\bMIL\b|^CTR\d?\s+(EHDL|EHVK|EHGR|EHLW|EHWO|EHDP|EHKD)\b'
# Areas only active when booked (NOTAM / airspace use plan)
INTERMITTENT_DEFAULT = r'EHTRA|EHTSA|<\s*notam\s*>'

CATEGORIES = ('violation', 'possible', 'unverified', 'exempt')

# Control zones a glider may cross with a clearance on request (Dutch Mil): inside is never a
# certain violation, the pilot may have had permission → always "possible".
CLEARANCE_DEFAULT = r'^CTR\d?\s+EHDL\b'

# Military aerodrome control zones: class D only while the aerodrome is open; when closed the
# CTR becomes an RMZ with class G ≤ 1500 ft and class E above (AIC 05/2023), i.e. no clearance.
# Opening hours known so far (weekdays, local time):
MIL_CTR_HOURS = [
    (r'^CTR\d?\s+EHDL', 8, 16, 'Deelen: weekdays about 08:00–16:00 local (MilAIP AD 2 EHDL: 0700–1500 UTC winter, '
                                '0600–1400 UTC summer; old copy — verify)'),
]


def _evening(h, off_day):
    return (h >= 18) & (h < 22)


def _weekday_or_evening(h, off_day):
    return np.ones(np.shape(h), bool) if not off_day else (h >= 18) & (h < 22)


# Time-dependent classification, AIP SUP 03/2022 (still current in AIRAC 09-2026) and the
# Regeling luchtverkeersdienstverlening (Staatscourant 2024, 8245). Each rule: airspace-name
# pattern, lower-limit pattern (None = any band), description, active(local hour, off_day).
TIME_RULES = [
    (r'^TMA\s*[345] LLS', None,
     'Lelystad TMA 3, 4, 5: class D 18:00–22:00 local every day, class E otherwise', _evening),
    (r'^TMA B MIL', None,
     'Nieuw Milligen TMA B (FL055–FL065): class D 18:00–22:00 local every day, class E otherwise', _evening),
    (r'^TMA (C2|E) MIL', r'FL0?55',
     'Nieuw Milligen TMA C2/E FL055–FL065: class D 18:00–22:00 local every day, class E otherwise', _evening),
    (r'^TMA (C2|E) MIL', r'FL0?65',
     'Nieuw Milligen TMA C2/E FL065–FL095: class D on weekdays; on weekends and public holidays only '
     '18:00–22:00 local, class E otherwise', _weekday_or_evening),
]


def time_rule_for(name: str, lower_text: str):
    """(description, active_fn) of the time-dependent classification of an airspace band, or None."""
    for pat, low, desc, fn in TIME_RULES:
        if re.search(pat, name, re.IGNORECASE) and (low is None or re.search(low, lower_text or '', re.IGNORECASE)):
            return desc, fn
    return None


def utc_offset_hours(datum) -> float:
    """Offset of Dutch local time from UTC on that date (1 in winter, 2 in summer)."""
    from src.config import TZ_LOCAL
    d = date.fromisoformat(str(datum)[:10])
    noon = datetime(d.year, d.month, d.day, 12, tzinfo=timezone.utc)
    return noon.astimezone(TZ_LOCAL).utcoffset().total_seconds() / 3600


def easter(year: int) -> date:
    """Gregorian Easter Sunday (Anonymous Gregorian algorithm)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7  # noqa: E741
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def nl_public_holidays(year: int) -> set:
    e = easter(year)
    kings = date(year, 4, 27)
    if kings.weekday() == 6:
        kings = date(year, 4, 26)
    return {date(year, 1, 1), e, e + timedelta(days=1), kings, date(year, 5, 5),
            e + timedelta(days=39), e + timedelta(days=49), e + timedelta(days=50),
            date(year, 12, 25), date(year, 12, 26)}


def is_off_day(d) -> bool:
    d = date.fromisoformat(str(d)[:10])
    return d.weekday() >= 5 or d in nl_public_holidays(d.year)


def _match(name: str, pattern: str) -> bool:
    return bool(pattern) and re.search(pattern, name, re.IGNORECASE) is not None


def is_part_time(name: str, pattern: str = PART_TIME_DEFAULT) -> bool:
    return _match(name, pattern)


def is_intermittent(name: str, pattern: str = INTERMITTENT_DEFAULT) -> bool:
    return _match(name, pattern)


def mil_ctr_hours(name: str):
    for pat, h0, h1, desc in MIL_CTR_HOURS:
        if re.search(pat, name, re.IGNORECASE):
            return h0, h1, desc
    return None


def notam_restricted(name: str, cls: str) -> bool:
    """Restricted areas without an H24 tag: activation is notified by NOTAM (AIP ENR 5.1 general rule)."""
    return (cls or '').upper() == 'R' and not re.search(r'<\s*h24\s*>', name, re.IGNORECASE)


def clearance_possible(name: str, pattern: str = CLEARANCE_DEFAULT) -> bool:
    return _match(name, pattern)


def category(name: str, datum, exempt_by, pattern=PART_TIME_DEFAULT, count_off_days=False,
             intermittent_pattern=INTERMITTENT_DEFAULT, count_intermittent=False, timed=False,
             cls=None, local_hour=None, clearance_pattern=CLEARANCE_DEFAULT) -> str:
    """'exempt', 'unverified', 'possible' or 'violation' (see module docstring)."""
    if exempt_by:
        return 'exempt'
    if timed:
        return 'violation'      # the published time rule already says it was class D at that moment
    if clearance_possible(name, clearance_pattern):
        return 'possible'       # crossing clearance can be requested (Dutch Mil)
    if not count_intermittent and (is_intermittent(name, intermittent_pattern) or notam_restricted(name, cls)):
        return 'unverified'
    if not count_off_days and is_part_time(name, pattern):
        if is_off_day(datum):
            return 'possible'
        hours = mil_ctr_hours(name)
        if hours is not None and local_hour is not None and not (hours[0] <= local_hour < hours[1]):
            return 'possible'
    return 'violation'
