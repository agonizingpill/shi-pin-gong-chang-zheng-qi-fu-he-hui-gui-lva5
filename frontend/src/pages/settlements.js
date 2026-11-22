import {PERIOD, SETTLE} from '../constants.js';
import {esc, clock, number} from '../utils.js';
import {feedback, heading, notice, report, status} from '../components/ui.js';

function monthStamp(month) {
  const [year, value] = month.month_key.split('-');
  return `<div class="shift-stamp"><strong>${esc(year)} 年 ${esc(Number(value))} 月</strong><span>入账 ${month.booked_hours} 小时 · 挂账 ${month.held_hours} 小时</span></div>`;
}

function hourTable(month) {
  if (!month.hours.length) return notice('本月无可结算小时', '方案审批后，目标班小时会按开始时间归入所属月份。');
  return `<div class="scroll settlement-scroll"><table><thead><tr><th>小时开始</th><th>班次</th><th class="num">实耗 kg</th><th class="num">基线 kg</th><th class="num">节能量 kg</th><th>归属 / 入账</th><th>状态</th><th>抄表依据</th></tr></thead>
    <tbody>${month.hours.map(row => `<tr>
      <td class="mono">${esc(row.hour_start.slice(5, 10))} ${clock(row.hour_start)}</td>
      <td>${esc(row.shift_code)}</td>
      <td class="num">${number(row.observed_kg)}</td>
      <td class="num">${number(row.baseline_kg)}</td>
      <td class="num">${Number(row.amount_kg) > 0 ? '+' : ''}${number(row.amount_kg)}</td>
      <td>${esc(row.attribution_month)}${row.booked_period && row.booked_period !== row.attribution_month ? `<br><small class="subtle">顺延 ${esc(row.booked_period)}</small>` : ''}</td>
      <td>${status(row.state, SETTLE)}</td>
      <td><small>#${row.start_read.id} ${esc(row.start_read.source_ref)}<br>#${row.end_read.id} ${esc(row.end_read.source_ref)}<br><span class="subtle">${number(row.start_read.cumulative_kg)} → ${number(row.end_read.cumulative_kg)} kg</span></small></td>
    </tr>`).join('')}</tbody></table></div>`;
}

function closeForm(month, role) {
  if (month.state !== 'open' || role !== 'supervisor') {
    return month.state === 'closed' ? notice('本月已关账', `关账于 ${esc(month.closed_at || '—')}${month.close_note ? ` · ${esc(month.close_note)}` : ''}。已入账金额不回写，后续差额顺延到下一个开放月。`) : '';
  }
  return `<form class="fields" data-path="/settlements/${esc(month.month_key)}/close"><label>关账说明<input name="note" minlength="4" maxlength="500" placeholder="核对已入账小时与顺延挂账" required></label>
    <button>关账 ${esc(month.month_key)}</button>${feedback}</form>`;
}

function monthDetail(month, role) {
  const sign = Number(month.booked_amount_kg) > 0 ? '+' : '';
  return `<section class="shift-paper"><div class="sheet-header"><div><p class="eyebrow">月度结算 / ${esc(month.month_key)}</p><h2>班组节能量</h2><span>节能量 = 获批基线 − 实耗，多耗以负数冲减</span></div>${status(month.state, PERIOD)}</div>
    <div class="gauges"><div><span>本月已入账</span><strong>${sign}${number(month.booked_amount_kg)}<small> kg / ${month.booked_hours} 小时</small></strong></div>
      <div><span>挂账待核</span><strong>${number(month.held_amount_kg)}<small> kg / ${month.held_hours} 小时</small></strong></div>
      <div><span>疑似仪表已排除</span><strong>${number(month.excluded_amount_kg)}<small> kg / ${month.excluded_hours} 小时</small></strong></div></div>
    ${month.held_hours ? notice('尖峰挂账', `有 ${month.held_hours} 小时（${number(month.held_amount_kg)} kg）待复核核验，暂不计入本月已入账；结案后按核验结论转入或排除。`) : ''}
    ${month.carried_hours ? notice('跨月顺延', `有 ${month.carried_hours} 小时（${number(month.carried_amount_kg)} kg）的归属月已关账，差额转入本月入账。`) : ''}
    <div class="capture-columns single"><section class="pane">${report('逐小时基线与抄表依据', '<span class="subtle">按小时开始时间归月 · kg</span>')}
      ${hourTable(month)}</section></div>
    <div class="sheet-footer">${closeForm(month, role)}</div></section>`;
}

export function settlementsPage(list, detail, role) {
  return `${heading('月度结算', '节能量结算', '按获批基线逐小时结算，挂账随复核结案转入所属月。')}
    <div class="desktop"><aside class="roster">${report('结算月份', `<span class="subtle">${list.length} 个月</span>`)}
      ${list.map(month => `<a class="roster-item ${month.month_key === detail?.month_key ? 'selected' : ''}" href="#settlements/${esc(month.month_key)}">${monthStamp(month)}${status(month.state, PERIOD)}</a>`).join('') || notice('尚无结算月份', '审批首个负荷方案后，目标班小时会自动归入月份。')}</aside>
    ${detail ? monthDetail(detail, role) : notice('选择结算月份', '从左侧选择月份，查看逐小时基线与入账情况。')}</div>`;
}
