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

`POST /v1/chart-of-accounts/validate`（`Content-Type: application/json`）对科目体系做无状态校验，请求须含非空字符串 `chart_id`、真实 `YYYY-MM-DD` 日历日期 `effective_date`，以及至少一项的 `accounts` 数组；顶层与科目层均拒绝未知字段。每个科目须含非空 `code`、`name`，枚举 `type`（`asset`/`liability`/`equity`/`revenue`/`expense`）与 `normal_balance`（`debit`/`credit`），布尔 `active`，以及字符串或 null 的 `parent_code`（null 表示根科目）。

关系规则：同版 `code` 唯一（`duplicate_account_code`）；非根 `parent_code` 须指向请求内另一个字段有效的科目，依次校验自引（`self_parent`）、父缺失（`parent_not_found`）、成环（`parent_cycle`，环内每项在各自 `parent_code` 路径报错）、父子类型一致（`parent_type_mismatch`）、启用子不得挂停用父（`inactive_parent`）；`asset`/`expense` 只能 `debit`、其余只能 `credit`，否则 `normal_balance_mismatch`。科目字段本身无效时不派生与之相关的关系错误。新增的枚举/数量错误码为 `invalid_account_type`、`invalid_normal_balance`、`too_few_accounts`，其余缺失、未知字段、类型、空值与日期错误沿用凭证校验的错误码。失败返回 422、`valid: false` 与同样按 `path`、`code` 排序的 `errors`。

成功返回 200、`valid: true`，原样带回 `chart_id`、`effective_date`，并给出整数 `account_count`、`root_count` 与覆盖五类数量的 `type_counts`。媒体类型、JSON 解析与顶层类型错误的状态码和错误体与凭证端点一致；该校验同样不落盘、不保留状态。

## 试算平衡表生成

`POST /v1/trial-balances/generate`（`Content-Type: application/json`）由期初余额与期间凭证无状态地生成试算平衡表。请求须含真实 `YYYY-MM-DD` 日历日期 `period_start`、`period_end`（先后有序，否则 `invalid_period`）、三位大写字母 `currency`、与科目体系校验同构的 `chart`、期初数组 `opening_balances`（每项含 `account_code`、`debit`、`credit`，仅一侧大于零，否则 `invalid_side`）以及与凭证校验同构的 `entries` 数组；顶层与期初项均拒绝未知字段。`chart` 与 `entries` 的字段级错误复用两个校验器，路径分别加 `/chart`、`/entries/{i}` 前缀（凭证不平衡记为 `/entries/{i}` 的 `unbalanced_entry`）。

跨对象规则：`chart.effective_date` 不得晚于 `period_start`（`chart_not_effective`）；凭证币种须与请求一致（`currency_mismatch`）；`posting_date` 须落在期间闭区间内（`posting_date_out_of_period`）；期初项与分录引用的科目须存在（`unknown_account`）且启用（`inactive_account`）；期初科目不得重复（`duplicate_opening_account`）；期初借贷总额须平衡（`unbalanced_opening_balances`）。字段无效时不派生依赖它的错误。失败返回 422、`valid: false` 与按 `path`、`code` 排序的 `errors`。

成功返回 200、`valid: true`，回显 `chart_id`、`period_start`、`period_end`、`currency`，并按 `account_code` 字典序返回 chart 全部科目的 `accounts` 行：每行含 `code`、`name`、`type`、`opening_debit`/`opening_credit`、`period_debit`/`period_credit`、`ending_debit`/`ending_credit`；期末为期初净额加期间净发生额抵销后仅落一侧，零额两侧均为 `0.00`。`totals` 汇总六个金额列，父子科目不重复汇总，每行只计自身余额。金额使用精确十进制并统一输出两位小数；媒体类型、JSON 解析与顶层类型错误的状态码和错误体与既有端点一致；处理不落盘、不保留状态。

## 验证

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

当前基线刻意不包含复式记账、报表生成与审计留痕的实现，以便后续任务从已冻结事实出发独立设计并验证这些能力。
