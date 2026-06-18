const UTC8_TIME_ZONE = 'Asia/Shanghai'

const dateTimeFormatter = new Intl.DateTimeFormat('zh-CN', {
  timeZone: UTC8_TIME_ZONE,
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
})

const timeFormatter = new Intl.DateTimeFormat('zh-CN', {
  timeZone: UTC8_TIME_ZONE,
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
})

const axisTimeFormatter = new Intl.DateTimeFormat('zh-CN', {
  timeZone: UTC8_TIME_ZONE,
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
})

function normalizeFormattedTime(value) {
  return String(value || '').replace(/\//g, '-')
}

function dateFromTs(ts) {
  if (!ts) return null
  const d = new Date(Number(ts))
  return Number.isNaN(d.getTime()) ? null : d
}

export function utc8DateTime(ts, fallback = '—') {
  const d = dateFromTs(ts)
  return d ? normalizeFormattedTime(dateTimeFormatter.format(d)) : (fallback || '—')
}

export function utc8Time(ts, fallback = '—') {
  const d = dateFromTs(ts)
  return d ? timeFormatter.format(d) : (fallback || '—')
}

export function utc8AxisTime(ts, fallback = '') {
  const d = dateFromTs(ts)
  return d ? normalizeFormattedTime(axisTimeFormatter.format(d)) : (fallback || '')
}

export function localTime(ts, fallback = '—') {
  return utc8DateTime(ts, fallback)
}

export function utc8InputToMs(value) {
  if (!value) return undefined
  const normalized = String(value).trim().replace(' ', 'T')
  const ts = Date.parse(`${normalized}+08:00`)
  return Number.isNaN(ts) ? undefined : ts
}

export function decisionLabel(action, skipped = false) {
  if (skipped) return '跳过 LLM'
  return {
    OPEN_LONG: '建议开多',
    OPEN_SHORT: '建议开空',
    CLOSE: '建议平仓',
    HOLD: '继续观望',
    ADJUST_SLTP: '调整止盈止损',
  }[action] || action || '—'
}

export function decisionTagType(action, skipped = false) {
  if (skipped) return 'info'
  return {
    OPEN_LONG: 'success',
    OPEN_SHORT: 'danger',
    CLOSE: 'primary',
    HOLD: 'warning',
    ADJUST_SLTP: 'primary',
  }[action] || 'info'
}

export function sideLabel(side) {
  return { buy: '买入', sell: '卖出' }[side] || side || '—'
}

export function orderKindTag(kind) {
  return { OPEN: 'success', CLOSE: 'primary', SL: 'danger', TP: 'warning' }[kind] || 'info'
}

export function orderActionLabel(row) {
  const kind = row?.client_kind
  const side = row?.side
  if (kind === 'OPEN') return side === 'buy' ? '开多' : side === 'sell' ? '开空' : '开仓'
  if (kind === 'CLOSE') return side === 'sell' ? '平多' : side === 'buy' ? '平空' : '平仓'
  if (kind === 'SL') return side === 'sell' ? '多单止损' : side === 'buy' ? '空单止损' : '止损'
  if (kind === 'TP') return side === 'sell' ? '多单止盈' : side === 'buy' ? '空单止盈' : '止盈'
  return kind || '—'
}

export function orderStatusLabel(row) {
  const status = row?.status
  const isCondition = row?.client_kind === 'SL' || row?.client_kind === 'TP'
  if (isCondition && status === 'placed') return '条件单成功挂出'
  if (isCondition && status === 'filled') return '条件单触发成交'
  if (isCondition && status === 'canceled') return '条件单已取消'
  if (isCondition && status === 'expired') return '条件单已过期'
  return {
    filled: '已成交',
    partial: '部分成交',
    placed: '已挂出',
    dry_run: '模拟记录',
    rejected: '已拒绝',
    error: '下单失败',
    canceled: '已取消',
    expired: '已过期',
  }[status] || status || '—'
}

export function orderStatusTag(row) {
  return {
    filled: 'success',
    partial: 'warning',
    placed: 'primary',
    dry_run: 'info',
    rejected: 'danger',
    error: 'danger',
    canceled: 'info',
    expired: 'warning',
  }[row?.status] || 'info'
}

export function orderTypeLabel(type) {
  return {
    market: '市价单',
    limit: '限价单',
    STOP_MARKET: '止损市价条件单',
    TAKE_PROFIT_MARKET: '止盈市价条件单',
  }[type] || type || '—'
}

export function executionModeLabel(mode) {
  return {
    MARKET_TAKER: '市价吃单',
    MAKER_ONLY: '只挂 maker',
    MAKER_FIRST: '优先 maker',
  }[mode] || mode || '—'
}

export function liquidityLabel(value) {
  return {
    maker: 'maker',
    taker: 'taker',
  }[value] || value || '—'
}

export function tradeDirectionLabel(direction) {
  return { long: '多单', short: '空单' }[direction] || direction || '—'
}

export function tradeDirectionTag(direction) {
  return { long: 'success', short: 'danger' }[direction] || 'info'
}

export function tradeStatusLabel(status) {
  return {
    open: '持仓中',
    closed: '已平仓',
    partial: '部分平仓',
    unmatched: '未匹配',
  }[status] || status || '—'
}

export function tradeStatusTag(status) {
  return {
    open: 'primary',
    closed: 'success',
    partial: 'warning',
    unmatched: 'info',
  }[status] || 'info'
}

export function exitReasonLabel(reason) {
  return {
    CLOSE: '策略平仓',
    TP: '止盈成交',
    SL: '止损成交',
    EMERGENCY: '保护平仓',
    CIRCUIT: '熔断平仓',
    UNKNOWN: '未知退出',
    EXTERNAL: '外部平仓',
  }[reason] || reason || '—'
}

export function tradeExitReasonLabel(row) {
  if (!row) return '—'
  const reason = row.exit_reason
  if (reason !== 'SL') return exitReasonLabel(reason)
  const direction = String(row.direction || '').toLowerCase()
  const entry = Number(row.entry_price || 0)
  const exit = Number(row.exit_price || 0)
  const netPnl = Number(row.net_realized_pnl ?? row.realized_pnl ?? 0)
  const totalFee = Math.abs(Number(row.total_fee || 0))
  const entryNotional = Math.abs(Number(row.entry_notional || 0))
  const tolerance = Math.max(totalFee, entryNotional * 0.0005, 0.01)
  const favorableExit = (
    (direction === 'long' && exit >= entry) ||
    (direction === 'short' && exit <= entry)
  )
  if (entry > 0 && exit > 0 && favorableExit && netPnl > tolerance) {
    return '移动止损成交'
  }
  if (entry > 0 && exit > 0 && favorableExit && netPnl >= -tolerance) {
    return '保本止盈'
  }
  return exitReasonLabel(reason)
}

export function rejectCodeLabel(code) {
  return {
    LOW_CONFIDENCE: '置信度不足',
    LEVERAGE_EXCEEDED: '杠杆超限',
    ORDER_NOTIONAL: '名义价值超限',
    ORDER_MARGIN: '单笔保证金超限',
    SYMBOL_MARGIN: '单标的保证金超限',
    TOTAL_MARGIN: '总保证金超限',
    LOSS_LIMIT: '单笔止损风险超限',
    LIQ_DISTANCE: '强平距离过近',
  }[code] || code || '—'
}

// LLM 调用耗时展示口径：
//   < 5s                → success
//   5 ~ 10s             → warning
//   10 ~ 20s            → danger
//   ≥ 20s 或 attempts≥3 → danger
//   status=degraded     → danger + 降级
//   latency_ms=0（无新版本数据）→ info，显示 "—"
const LLMLatencyBucket = {
  fast: 5000,   // 5s
  slow: 10000,  // 10s
  dead: 20000,  // 20s
}

export function llmLatencyTag(row) {
  if (!row) return { type: 'info', label: '—' }
  const status = String(row.llm_status || '')
  const latency = Number(row.llm_latency_ms || 0)
  const attempts = Number(row.llm_attempts || 0)
  if (!status) return { type: 'info', label: '—' }
  if (status === 'degraded') {
    return { type: 'danger', label: attempts > 1 ? `降级 ×${attempts}` : '降级' }
  }
  if (latency <= 0) return { type: 'info', label: '—' }
  let bucket = 'success'
  if (latency >= LLMLatencyBucket.dead || attempts >= 3) bucket = 'danger'
  else if (latency >= LLMLatencyBucket.slow) bucket = 'danger'
  else if (latency >= LLMLatencyBucket.fast) bucket = 'warning'
  const seconds = (latency / 1000).toFixed(1)
  return {
    type: bucket,
    label: attempts > 1 ? `${seconds}s ×${attempts}` : `${seconds}s`,
  }
}
