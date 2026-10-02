# LedgerLens

这是一个面向财务报表与会计分析的财务报表与会计分析引擎。长期目标是提供复式记账与科目体系、凭证校验、期间结账、多币种折算、三大报表生成、估值模型、审计留痕和报表对账，把财务分析沉淀为可复用引擎。

仓库采用 Python，当前冻结基线只提供进程健康检查。后续能力必须通过独立题目逐步实现；每个题目都应定义可观察的公共行为、兼容边界和失败语义，不得依赖未公开内部 API。

## 启动

```bash
PYTHONPATH=src python3 -m ledgerlens.server --host 127.0.0.1 --port 8080
```

服务默认监听 `127.0.0.1:8080`，可通过 `LEDGERLENS_ADDR` 修改。`GET /healthz` 返回 JSON 健康状态。

## 凭证校验

`POST /v1/journal-entries/validate`（`Content-Type: application/json`）对复式记账凭证做无状态校验：不落盘、不保留跨请求状态，相同输入返回相同结果。借贷平衡时返回 200 与规范化为两位小数的 `debit_total`/`credit_total`；不平衡返回 422 与 `code: "unbalanced_entry"`；结构与字段错误返回 422 与按 `path`、`code` 字典序排列的 `errors` 列表（每项含 JSON Pointer 形式的 `path`、`code`、`message`）。媒体类型不符返回 415 `unsupported_media_type`，JSON 解析失败返回 400 `invalid_json`，顶层非对象返回 400 `request_not_object`。

## 科目体系校验

`POST /v1/chart-of-accounts/validate`（`Content-Type: application/json`）对一套会计科目做同样无状态的校验。请求必填非空 `chart_id`、真实 `YYYY-MM-DD` 的 `effective_date`，以及至少一项的 `accounts`。每个科目必填非空 `code`、`name`，`type` 取 `asset`、`liability`、`equity`、`revenue`、`expense`，`normal_balance` 取 `debit`、`credit`，`active` 为布尔，`parent_code` 为字符串或 `null`；顶层与科目均拒绝未知字段。

同版 `code` 唯一；`parent_code` 为 `null` 表示根科目，否则须指向请求内另一有效科目，不得自引或成环（环内每项在其 `parent_code` 路径报错），子科目与父科目同 `type`，启用子科目不得挂在停用父科目下；`asset`、`expense` 只能为 `debit`，其余只能为 `credit`。科目过少及两个枚举错误分别为 `too_few_accounts`、`invalid_account_type`、`invalid_normal_balance`，关系错误依次为 `duplicate_account_code`、`parent_not_found`、`self_parent`、`parent_cycle`、`parent_type_mismatch`、`inactive_parent`、`normal_balance_mismatch`；字段无效时不派生相关关系错误。

成功返回 200、`valid: true`，原样带回 `chart_id`、`effective_date`，并给出整数 `account_count`、`root_count` 与覆盖五类数量的 `type_counts`；失败返回 422、`valid: false` 与沿用凭证端点结构和排序的 `errors`。媒体类型、JSON 解析与顶层类型错误的状态码和错误体与凭证端点一致。

## 验证

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

当前基线刻意不包含复式记账、报表生成与审计留痕的实现，以便后续任务从已冻结事实出发独立设计并验证这些能力。
