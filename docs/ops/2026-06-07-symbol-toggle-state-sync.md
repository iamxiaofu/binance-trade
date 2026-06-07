# 2026-06-07 币种交易开关状态同步

## 背景

Web 操作面板的“币种交易开关”用于下发 `SET_SYMBOL_ENABLED` 命令，
由交易主进程消费后写入 `symbol.enabled.<SYMBOL>` 运行态配置。

实际使用中发现：停用 `BNBUSDT` 后，命令已经入队并由交易进程执行，
但页面上的状态标签和操作按钮没有及时从“已启用 / 停用交易”切换到
“已停用 / 启用交易”。用户需要等待后续刷新或手动刷新页面，才能看到最新状态。

## 根因

操作面板的币种状态来自 `/api/config` 返回的 `symbol_enabled`。

旧逻辑在单币种 `SET_SYMBOL_ENABLED` 下发成功后只刷新命令历史：

- 命令历史能看到 pending/done。
- 本地 `cfg.symbol_enabled` 没有立即更新。
- 命令完成后也没有针对配置状态重新拉取 `/api/config`。

因此按钮和状态标签会继续使用旧的配置快照，直到下一次完整刷新。

## 改造内容

- `SET_SYMBOL_ENABLED` 下发成功后，前端立即更新本地 `cfg.symbol_enabled`。
- 币种状态标签和操作按钮会立即切换。
- 前端短轮询命令历史，等待该命令进入 `done` 或 `failed`。
- 命令完成后重新拉取 `/api/config`，以运行态持久化状态为准。
- 实时 summary 推送中出现配置类命令完成时，也会触发配置刷新。

## 状态同步流程

1. Web 端仍只写命令队列，不直接操作交易所。
2. 命令入队成功后，页面先做本地乐观更新。
3. 交易主进程消费 `SET_SYMBOL_ENABLED`，写入 `symbol.enabled.<SYMBOL>`。
4. 前端检测命令进入 `done` 或 `failed` 后，重新加载 `/api/config`。
5. 若短时间内没有确认命令结果，页面会重新刷新命令和配置，并提示稍后刷新。

## 涉及文件

- `web/frontend/src/views/Control.vue`

## 验证

执行：

```bash
cd web/frontend
npm run build
```

结果：

```text
✓ built
```

构建过程中仍有 `@vueuse/core` 的 Rolldown `INVALID_ANNOTATION` 警告，
与既有构建日志一致，不影响产物生成。

## 运维注意

- 已打开的浏览器页面需要刷新，才能加载新的前端 chunk。
- 若命令执行失败，页面会重新拉取后端配置并提示失败原因。
- 页面展示会先响应用户操作，但最终状态以 `/api/config` 读取到的运行态持久化值为准。
