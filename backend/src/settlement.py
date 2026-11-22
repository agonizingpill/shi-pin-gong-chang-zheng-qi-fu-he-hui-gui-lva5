"""Monthly settlement of approved baselines.

Every hour of an approved plan is settled once, attributed to the calendar
month of the hour's start (a shift may straddle midnight and month ends).
Hours flagged as target-shift spikes are held until the energy review is
closed; instrument findings are excluded, everything else is booked into the
attribution month or, when that month is already closed, the next open month.
"""
import re
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError

from .auth import DomainError
from .config import utc_now
from .constants import PeriodState, PlanState, ReviewState, Role, SettlementState, SpikeFinding
from .models import (
    EnergyReview, LoadPlan, ReviewSpike, SettlementHour, SettlementPeriod, ProductionShift, SteamMeter, User,
)
from .shift_work import audit

CENT = Decimal('0.01')
MONTH_KEY = re.compile(r'^\d{4}-(0[1-9]|1[0-2])$')


def normalize_month(value):
    value = (value or '').strip()
    if not MONTH_KEY.match(value):
        raise DomainError('invalid_month', '月份须为 YYYY-MM 格式', 422)
    return value


def month_of(moment):
    return moment.strftime('%Y-%m')


def next_month(month_key):
    year, month = (int(part) for part in month_key.split('-'))
    return f'{year + 1:04d}-01' if month == 12 else f'{year:04d}-{month + 1:02d}'


def lock_period(db, month_key):
    """Return the month row under a row lock, creating it when absent.

    The lock serializes month close with bookings so a close either sees the
    booking or makes the booking roll to the next open month; never both.
    """
    row = db.scalar(select(SettlementPeriod).where(SettlementPeriod.month_key == month_key).with_for_update())
    if row is not None:
        return row
    try:
        with db.begin_nested():
            db.add(SettlementPeriod(month_key=month_key))
            db.flush()
    except IntegrityError:
        pass
    row = db.scalar(select(SettlementPeriod).where(SettlementPeriod.month_key == month_key).with_for_update())
    if row is None:
        raise DomainError('period_unavailable', '无法建立结算月份，请稍后重试', 503)
    return row


def booking_month(db, attribution, cache):
    """First open month at or after the attribution month."""
    if attribution in cache:
        return cache[attribution]
    candidate = attribution
    for _attempt in range(2400):
        period = lock_period(db, candidate)
        if period.state != PeriodState.CLOSED:
            cache[attribution] = candidate
            return candidate
        candidate = next_month(candidate)
    raise DomainError('no_open_period', '找不到可入账的开放月份', 409)


def book_hour(db, row, cache):
    target = booking_month(db, row.attribution_month, cache)
    row.state = SettlementState.BOOKED
    row.booked_period = target
    row.booked_at = utc_now()
    return target


def target_reads(plan):
    return {row['start']: row for row in plan.input_snapshot['target']}


def book_plan(db, plan, review, user):
    """Book non-spike target hours immediately; hold target spikes."""
    profile = {row['start']: row for row in (plan.result or {}).get('profile', [])}
    target = target_reads(plan)
    if not profile or set(profile) != set(target):
        raise DomainError('settlement_mismatch', '方案曲线与输入快照小时不一致，无法入账')
    spikes = list(db.scalars(select(ReviewSpike).where(
        ReviewSpike.review_id == review.id, ReviewSpike.sample_scope == 'target')))
    by_reads = {(item.start_read_id, item.end_read_id): item for item in spikes}
    cache = {}
    booked = held = 0
    total = Decimal('0')
    for start in sorted(target):
        source = target[start]
        curve = profile[start]
        left = db.get(SteamMeter, source['start_read_id'])
        right = db.get(SteamMeter, source['end_read_id'])
        if left is None or right is None:
            raise DomainError('settlement_mismatch', '目标班抄表依据缺失，无法入账')
        observed = (Decimal(right.cumulative_kg) - Decimal(left.cumulative_kg)).quantize(CENT)
        baseline = Decimal(str(curve['baseline_kg'])).quantize(CENT)
        amount = (baseline - observed).quantize(CENT)
        spike = by_reads.get((source['start_read_id'], source['end_read_id']))
        row = SettlementHour(
            plan_id=plan.id, shift_id=plan.shift_id,
            hour_start=datetime.fromisoformat(start), attribution_month=month_of(datetime.fromisoformat(start)),
            observed_kg=observed, baseline_kg=baseline, amount_kg=amount,
            start_read_id=source['start_read_id'], end_read_id=source['end_read_id'],
            spike_id=spike.id if spike else None,
            state=SettlementState.HELD,
        )
        db.add(row)
        if spike is not None:
            held += 1
        else:
            book_hour(db, row, cache)
            booked += 1
            total += amount
    db.flush()
    audit(db, user.username, 'settlement.booked', plan.id, shift=plan.shift_id,
          hours=len(target), booked=booked, held=held, amount_kg=str(total))


def resolve_review(db, review, user):
    """Exclude instrument suspects; release remaining holds to an open month."""
    rows = list(db.scalars(select(SettlementHour).where(
        SettlementHour.plan_id == review.plan_id, SettlementHour.state == SettlementState.HELD,
    ).order_by(SettlementHour.hour_start).with_for_update()))
    spikes = {item.id: item for item in db.scalars(select(ReviewSpike).where(ReviewSpike.review_id == review.id))}
    cache = {}
    excluded = released = 0
    total = Decimal('0')
    now = utc_now()
    for row in rows:
        spike = spikes.get(row.spike_id)
        if spike is not None and spike.finding == SpikeFinding.INSTRUMENT_SUSPECT:
            row.state = SettlementState.EXCLUDED
            row.excluded_by, row.excluded_at = user.id, now
            row.exclusion_note = spike.findings_note
            excluded += 1
        else:
            book_hour(db, row, cache)
            released += 1
            total += row.amount_kg
    db.flush()
    audit(db, user.username, 'settlement.released', review.id, plan=review.plan_id,
          released=released, excluded=excluded, amount_kg=str(total))


def close_month(db, month_key, data, user):
    """Close a month idempotently. Bookings are already persisted per hour."""
    month_key = normalize_month(month_key)
    period = lock_period(db, month_key)
    if period.state == PeriodState.CLOSED:
        return period
    period.state, period.closed_by, period.closed_at = PeriodState.CLOSED, user.id, utc_now()
    period.close_note = data.note
    audit(db, user.username, 'settlement.period_closed', month_key, note=data.note)
    return period


def backfill(db):
    """Settle approved plans that predate this feature. Idempotent per plan."""
    settled = select(SettlementHour.id).where(SettlementHour.plan_id == LoadPlan.id)
    orphans = list(db.scalars(select(LoadPlan).where(
        LoadPlan.state == PlanState.APPROVED,
        LoadPlan.result.is_not(None),
        ~settled.exists(),
    ).order_by(LoadPlan.id).with_for_update(skip_locked=True)))
    if not orphans:
        return 0
    reviewer = db.scalar(select(User).where(User.role == Role.SUPERVISOR).order_by(User.id).limit(1))
    if reviewer is None:
        raise DomainError('settlement_backfill_blocked', '缺少能源主管账号，无法补记历史方案结算', 503)
    count = 0
    for plan in orphans:
        review = db.scalar(select(EnergyReview).where(EnergyReview.plan_id == plan.id))
        if review is None:
            continue
        audit(db, 'system', 'settlement.backfill', plan.id, shift=plan.shift_id)
        book_plan(db, plan, review, reviewer)
        if review.state == ReviewState.CLOSED:
            resolve_review(db, review, reviewer)
        count += 1
    return count


def _sum_hours(db, *conditions):
    row = db.execute(select(func.coalesce(func.sum(SettlementHour.amount_kg), 0),
                            func.count(SettlementHour.id)).where(*conditions)).one()
    return {'amount_kg': f'{Decimal(row[0]).quantize(CENT)}', 'hours': int(row[1])}


def month_summary(db, month_key):
    period = db.scalar(select(SettlementPeriod).where(SettlementPeriod.month_key == month_key))
    booked = _sum_hours(db, SettlementHour.booked_period == month_key,
                        SettlementHour.state == SettlementState.BOOKED)
    held = _sum_hours(db, SettlementHour.attribution_month == month_key,
                      SettlementHour.state == SettlementState.HELD)
    excluded = _sum_hours(db, SettlementHour.attribution_month == month_key,
                          SettlementHour.state == SettlementState.EXCLUDED)
    carried = _sum_hours(db, SettlementHour.booked_period == month_key,
                         SettlementHour.attribution_month != month_key,
                         SettlementHour.state == SettlementState.BOOKED)
    return {
        'id': period.id if period else None,
        'month_key': month_key,
        'state': period.state if period else PeriodState.OPEN,
        'closed_at': period.closed_at.isoformat(timespec='minutes') if period and period.closed_at else None,
        'close_note': period.close_note if period else None,
        'booked_amount_kg': booked['amount_kg'],
        'booked_hours': booked['hours'],
        'held_amount_kg': held['amount_kg'],
        'held_hours': held['hours'],
        'excluded_amount_kg': excluded['amount_kg'],
        'excluded_hours': excluded['hours'],
        'carried_amount_kg': carried['amount_kg'],
        'carried_hours': carried['hours'],
    }


def list_months(db):
    keys = set(db.scalars(select(SettlementPeriod.month_key)))
    keys.update(db.scalars(select(SettlementHour.attribution_month).distinct()))
    keys.update(item for item in db.scalars(select(SettlementHour.booked_period).distinct()) if item)
    return [month_summary(db, key) for key in sorted(keys, reverse=True)]


def month_hours(db, month_key):
    return list(db.scalars(select(SettlementHour).where(or_(
        SettlementHour.booked_period == month_key,
        (SettlementHour.state != SettlementState.BOOKED) & (SettlementHour.attribution_month == month_key),
    )).order_by(SettlementHour.hour_start, SettlementHour.id)))


def hour_view(db, row):
    start = db.get(SteamMeter, row.start_read_id)
    end = db.get(SteamMeter, row.end_read_id)
    shift = db.get(ProductionShift, row.shift_id)
    plan = db.get(LoadPlan, row.plan_id)
    def meter(item):
        return {'id': item.id, 'reading_at': item.reading_at.isoformat(timespec='minutes'),
                'cumulative_kg': f'{Decimal(item.cumulative_kg).quantize(CENT)}', 'source_ref': item.source_ref}
    return {
        'id': row.id, 'plan_id': row.plan_id, 'plan_title': plan.title if plan else None,
        'shift_id': row.shift_id, 'shift_code': shift.code if shift else None,
        'hour_start': row.hour_start.isoformat(timespec='minutes'),
        'attribution_month': row.attribution_month,
        'booked_period': row.booked_period,
        'state': row.state,
        'observed_kg': f'{Decimal(row.observed_kg).quantize(CENT)}',
        'baseline_kg': f'{Decimal(row.baseline_kg).quantize(CENT)}',
        'amount_kg': f'{Decimal(row.amount_kg).quantize(CENT)}',
        'start_read': meter(start), 'end_read': meter(end),
        'spike_id': row.spike_id,
        'booked_at': row.booked_at.isoformat(timespec='minutes') if row.booked_at else None,
        'excluded_at': row.excluded_at.isoformat(timespec='minutes') if row.excluded_at else None,
        'exclusion_note': row.exclusion_note,
    }


def month_view(db, month_key):
    month_key = normalize_month(month_key)
    result = month_summary(db, month_key)
    result['hours'] = [hour_view(db, row) for row in month_hours(db, month_key)]
    return result
