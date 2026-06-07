# 2026-06-07 决策日志筛选能力

## 背景

Web 决策日志用于查看每个周期、每个币种的 LLM 决策与跳过原因。

旧页面固定读取最近 150 条记录，不支持按币种、时间范围或类型筛选。
当某个币种被停用后，交易主循环会持续记录 `skip_reason=symbol disabled`，
这些记录对审计有价值，但日常排查时会挤占大量列表空间，影响查看真实决策。

## 根因

旧接口 `/api/decisions` 只支持 `limit`，后端直接按 `id DESC` 返回最近记录。

如果在前端本地过滤，会出现两个问题：

- 只能过滤已经拉到浏览器的最近 N 条，过滤后可能误以为没有有效决策。
- 分页和总数不准确，后续扩展导出或更多筛选条件会继续受限。

因此筛选必须放到服务端 SQL 查询层，由后端返回准确的结果集和总数。

## 改造内容

- `/api/decisions` 支持服务端筛选与分页。
- 决策日志页面新增筛选栏：
  - 币种
  - 时间范围
  - 类型
  - 忽略停用币种日志
- 表格和详情中的“标的”文案改为“币种”。
- 前端 API 查询参数支持数组，便于多选筛选复用。

## 查询接口

`GET /api/decisions` 支持以下查询参数：

- `symbol`：可重复传入，按多个币种筛选。
- `type`：可重复传入，支持 `SKIPPED`、`OPEN_LONG`、`OPEN_SHORT`、`CLOSE`、`HOLD`。
- `start_ts_ms`：开始时间，毫秒 epoch。
- `end_ts_ms`：结束时间，毫秒 epoch。
- `hide_symbol_disabled`：为 `true` 时隐藏 `skip_reason=symbol disabled` 的跳过记录。
- `limit`：每页条数，最大 500。
- `offset`：分页偏移量。

响应结构：

```json
{
  "items": [],
  "total": 0,
  "limit": 100,
  "offset": 0,
  "filters": {}
}
```

## 页面行为

- 点击“查询类”筛选项后重置到第一页。
- “忽略停用币种日志”是快捷过滤按钮，不删除数据库记录。
- 关闭该按钮后仍可查看完整审计日志。
- 页面分页基于服务端 `total`，不会受前端本地过滤影响。

## 涉及文件

- `web/status.py`
- `web/server.py`
- `web/frontend/src/api.js`
- `web/frontend/src/views/Decisions.vue`
- `tests/test_web_status.py`

## 验证

执行：

```bash
.venv/bin/python -m pytest tests/test_web_status.py
cd web/frontend
npm run build
```

期望结果：

```text
tests/test_web_status.py passed
✓ built
```

构建过程中若出现 `@vueuse/core` 的 Rolldown `INVALID_ANNOTATION` 警告，
与既有构建日志一致，不影响产物生成。

## 运维注意

- 本次改造包含 Web 后端接口变更，部署后需要重启 `binance-trade-web.service`。
- 已打开的浏览器页面需要刷新，才能加载新的前端 chunk。
- “忽略停用币种日志”只影响页面查询，不影响交易主进程继续写入审计记录。
