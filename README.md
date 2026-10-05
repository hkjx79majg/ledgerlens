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

## 财务报表生成

`POST /v1/financial-statements/generate`（`Content-Type: application/json`）由同一账务输入无状态地生成损益表与资产负债表。请求契约、字段级与跨对象校验、错误结构、状态码与排序规则完全沿用试算平衡表端点；媒体类型不符返回 415 `unsupported_media_type`，JSON 解析失败返回 400 `invalid_json`，顶层非对象返回 400 `request_not_object`，业务校验失败返回 422、`valid: false` 与按 `path`、`code` 排序的 `errors`。

成功返回 200、`valid: true`，回显 `chart_id`、`period_start`、`period_end`、`currency`，并附 `income_statement` 与 `balance_sheet`。损益表按 `account_code` 字典序在 `revenue`、`expense` 分组列出全部收入、费用科目，每行含 `account_code`、`name`、`amount`：收入为本期贷方发生额减借方发生额，费用为本期借方发生额减贷方发生额，并汇总 `total_revenue`、`total_expense`、`net_income`。资产负债表按相同顺序在 `assets`、`liabilities`、`equity` 分组列出全部资产、负债、权益科目，每行字段相同：资产为期末借方余额减贷方余额，负债和权益取相反方向；另返回 `total_assets`、`total_liabilities`、`total_equity_before_net_income`、`current_period_net_income`（等于 `net_income`）、`total_liabilities_and_equity`（负债、期末既有权益与本期利润之和）与 `balanced`（仅在其与 `total_assets` 精确相等时为 true）。父子科目均只展示自身金额，不作层级滚算；金额统一输出两位小数字符串，反向余额保留负号，零值科目仍保留；处理不落盘、不保留状态，相同输入结果一致。

## 现金流量表生成

`POST /v1/cash-flow-statements/generate`（`Content-Type: application/json`）在财务报表同一账务输入上新增两个顶层字段，无状态地生成现金流量表。`cash_account_codes` 为非空、无重复的科目代码数组：非数组报 `invalid_type`，空数组报 `too_few_cash_accounts`，元素非字符串报 `invalid_type`、空字符串报 `blank_value`、重复报 `duplicate_cash_account`；每个代码还须依次通过存在（`unknown_account`）、启用（`inactive_account`）、资产类（`cash_account_not_asset`）校验。`entry_activities` 为与 `entries` 等长的数组，元素仅可为 `operating`/`investing`/`financing` 或 null：非数组报 `invalid_type`，长度不符报 `activity_count_mismatch`，取值非法报 `invalid_cash_flow_activity`。凭证现金净变动非零却配 null 报 `missing_cash_flow_activity`，净变动为零却配活动报 `activity_without_cash_change`；依赖字段或凭证本身无效时不派生这些关联错误。

既有字段沿用试算平衡表端点的全部校验，顶层拒绝未知字段（因此旧端点仍拒绝这两个新字段）。失败返回 422、`valid: false` 与按 `path`、`code` 排序的 `errors`；媒体类型、JSON 解析与顶层类型错误的状态码和错误体与既有端点一致。

指定现金科目的期初余额与每张凭证的现金变动均按「借方减贷方」计算，正为流入、负为流出；父子科目不滚算，只计显式指定的科目。成功返回 200、`valid: true`，回显 `chart_id`、`period_start`、`period_end`、`currency`，并附 `cash_flow_statement`：按 `operating`、`investing`、`financing` 分区，各区含 `items` 与 `total`；`items` 仅列现金变动非零的凭证并保持 `entries` 顺序，每项含 `voucher_id`、`posting_date` 与两位小数带符号 `amount`。另返回 `beginning_cash_balance`、`net_cash_change`、`ending_cash_balance`：三区合计等于净变动，期初加净变动等于期末。金额使用精确十进制；处理不落盘、不保留状态，相同输入结果一致。

## 期末损益结转

`POST /v1/period-closes/generate`（`Content-Type: application/json`）在试算平衡表的同一账务输入上新增两个非空字符串顶层字段，无状态地生成期末损益结转凭证与下一期期初余额：`retained_earnings_account_code` 须指向存在（`unknown_account`）、启用（`inactive_account`）的 equity 科目（否则 `retained_earnings_not_equity`，三类依次只报一个）；`closing_voucher_id` 与任一 `entries` 中字段有效的 `voucher_id` 相同时报 `duplicate_voucher_id`。收入或费用科目带非零期初余额时，在对应 `/opening_balances/{i}` 报 `nonzero_temporary_opening_balance`。关联错误只依赖自身字段有效：科目体系无效时不派生留存收益与期初类错误，期初项金额字段无效、凭证 `voucher_id` 字段无效时不派生对应关联错误。既有字段沿用试算平衡表端点的全部校验、错误抑制与排序（旧端点仍拒绝这两个新字段）；缺失、类型、空值错误沿用既有错误码。失败返回 422、`valid: false` 与按 `path`、`code` 排序的 `errors`；媒体类型、JSON 解析与顶层类型错误的状态码和错误体与既有端点一致。

成功返回 200、`valid: true`，回显 `chart_id`、`period_start`、`period_end`、`currency`，并附 `net_income`、`closing_entry`、`next_opening_balances`。结转只取本期发生额：非零收入按「贷方减借方」的反方向（借记收入）清零，非零费用按「借方减贷方」的反方向（贷记费用）清零；`net_income` 为收入合计减费用合计，正数贷记留存收益、负数借记、零值不生成该行。`closing_entry` 使用请求的 `closing_voucher_id`、`posting_date` 取 `period_end`、`currency` 取请求币种；损益行按 `account_code` 字典序排列、留存收益行置后，`line_id` 依次为 `close-1`、`close-2`……每行金额为非负两位小数字符串且仅一侧大于零，借贷合计精确相等；无非零损益发生额时 `closing_entry` 为 `null`。`next_opening_balances` 只含资产、负债、权益科目的期末净额（留存收益叠加本期净利润），按 `account_code` 字典序排列，非零余额抵销后仅落一侧、零余额省略，父子科目只计自身。全部金额使用精确十进制并统一输出两位小数；处理不落盘、不保留状态，相同输入结果一致。

## 权责发生确认计划

`POST /v1/recognition-schedules/generate`（`Content-Type: application/json`）无状态地生成待摊费用或递延收入的月度确认计划与复式分录。请求含十个顶层字段，拒绝未知字段：`contract_id`、`voucher_id_prefix` 为非空字符串；`recognition_type` 只接受 `revenue` 或 `expense`，否则报 `invalid_recognition_type`；`start_date`、`end_date` 为真实日历日期且首尾日均计入天数，`start_date` 晚于 `end_date` 时在 `/start_date` 报 `invalid_period`；`currency` 为三位大写字母；`total_amount` 须为大于零且最多两位小数的无符号字符串，否则报 `invalid_amount`；`chart` 与科目体系校验同构（错误路径加 `/chart` 前缀），且 `effective_date` 不得晚于 `start_date`（`chart_not_effective`）；`source_account_code`、`target_account_code` 为非空字符串，两者相同时在 `/target_account_code` 报 `duplicate_recognition_account`。两科目分别按存在（`unknown_account`）、启用（`inactive_account`）、类别（`account_type_mismatch`）依次只报一个：revenue 要求来源为 liability、目标为 revenue，expense 要求来源为 asset、目标为 expense。关联错误只依赖自身字段有效：科目体系无效时不派生科目类错误，`recognition_type` 无效时不派生类别错误。缺失、类型、空值、日期、币种错误沿用既有错误码；失败返回 422、`valid: false` 与按 `path`、`code` 排序的 `errors`；媒体类型、JSON 解析与顶层类型错误的状态码和错误体与既有端点一致。

成功返回 200、`valid: true`，回显 `chart_id`、`contract_id`、`recognition_type`、`start_date`、`end_date`、`currency` 与两位小数 `total_amount`，并附日期升序的 `recognition_schedule`。区间按自然月切段，每项含 `period_start`、`period_end`、首尾计入的天数 `days`、两位小数 `amount` 与 `entry`；各段按天数占比以精确十进制分摊，份额先向下取整到分，剩余分按小数余数从大到小各补一分、余数相同时较早月份优先，各项金额之和严格等于 `total_amount`。`entry` 以分段末日为 `posting_date`、以 `{voucher_id_prefix}-{YYYYMM}` 为 `voucher_id`、币种取请求币种；revenue 模式借记来源、贷记目标，expense 模式借记目标、贷记来源，`line_id` 依次为 `rec-1`、`rec-2`，每行仅一侧大于零，借贷合计精确相等，每张凭证均可通过既有凭证校验。分摊为 `0.00` 的分段不生成计划项。处理不落盘、不保留状态，相同输入结果一致；既有公开方法与路由行为不变。

## 固定资产直线法折旧计划

`POST /v1/depreciation-schedules/generate`（`Content-Type: application/json`）无状态地生成固定资产直线法月度折旧计划与复式分录。请求含十个顶层字段，拒绝未知字段：`asset_id`、`voucher_id_prefix` 为非空字符串；`in_service_date` 为真实 `YYYY-MM-DD` 日历日期；`currency` 为三位大写字母；`acquisition_cost`、`residual_value` 为至多两位小数的无符号十进制字符串，成本须大于零、残值须不小于零，否则报 `invalid_amount`；残值不小于成本时在 `/residual_value` 报 `residual_not_less_than_cost`；`useful_life_months` 须为 1 至 1200 的整数（布尔不算整数），范围外报 `invalid_useful_life`。`chart` 与科目体系校验同构（错误路径加 `/chart` 前缀），且 `effective_date` 不得晚于 `in_service_date`（`chart_not_effective`）；`accumulated_depreciation_account_code`（累计折旧）、`depreciation_expense_account_code`（折旧费用）为非空字符串，两者相同时在 `/depreciation_expense_account_code` 报 `duplicate_depreciation_account`。两科目分别按存在（`unknown_account`）、启用（`inactive_account`）、类别（`account_type_mismatch`）依次只报一个：累计折旧科目须为 `asset`，折旧费用科目须为 `expense`。末月计提日超出日历范围（9999-12-31）时在 `/in_service_date` 报 `schedule_out_of_range`。关联错误只依赖自身字段有效：科目体系无效时不派生科目类错误，金额字段无效时不派生残值比较，月数或启用日无效时不派生范围错误。缺失、类型、空值、日期、币种错误沿用既有错误码；失败返回 422、`valid: false` 与按 `path`、`code` 排序的 `errors`；媒体类型、JSON 解析与顶层类型错误的状态码和错误体与既有端点一致。

成功返回 200、`valid: true`，回显 `chart_id`、`asset_id`、`in_service_date`、`currency`、两位小数的 `acquisition_cost`/`residual_value` 与 `useful_life_months`，并附日期升序的 `depreciation_schedule`。自启用日所在月末起按整月连续计提指定月数（与启用日为当月何日无关）；可折旧金额（成本减残值）按整数分平均分配，每月先取等额商，余数从最早月份起各补一分。每项含 `posting_date`（当月月末）、两位小数 `amount`（本月折旧）、`accumulated_depreciation`（截至当月累计折旧）、`net_book_value`（账面净值）与 `entry`；期末累计折旧严格等于成本减残值、账面净值等于残值。非零月份的 `entry` 以当月月末为 `posting_date`、以 `{voucher_id_prefix}-{YYYYMM}` 为 `voucher_id`、币种取请求币种，`dep-1` 借记折旧费用科目、`dep-2` 贷记累计折旧科目，每行仅一侧大于零，借贷合计精确相等且可通过既有凭证校验；本月金额为 `0.00` 的月份仍保留在计划中且 `entry` 为 `null`。金额使用精确十进制；处理不落盘、不保留状态，相同输入结果一致；既有公开方法与路由行为不变。

## 固定资产减值测算

`POST /v1/asset-impairments/generate`（`Content-Type: application/json`）无状态地进行固定资产减值测算并生成复式分录。请求含九个顶层字段，拒绝未知字段：`asset_id`、`voucher_id` 为非空字符串；`test_date` 为真实 `YYYY-MM-DD` 日历日期；`currency` 为三位大写字母；`carrying_amount`、`recoverable_amount` 为至多两位小数的无符号十进制字符串，账面金额须大于零、可收回金额须不小于零，否则报 `invalid_amount`。`chart` 与科目体系校验同构（错误路径加 `/chart` 前缀），且 `effective_date` 不得晚于 `test_date`（`chart_not_effective`）；`accumulated_impairment_account_code`（累计减值）、`impairment_loss_account_code`（减值损失）为非空字符串，两者相同时在 `/impairment_loss_account_code` 报 `duplicate_impairment_account`。两科目分别按存在（`unknown_account`）、启用（`inactive_account`）、类别（`account_type_mismatch`）依次只报一个：累计减值科目须为 `asset`，减值损失科目须为 `expense`。关联错误只依赖自身字段有效：科目体系无效时不派生科目类错误。缺失、类型、空值、日期、币种错误沿用既有错误码；失败返回 422、`valid: false` 与按 `path`、`code` 排序的 `errors`；媒体类型、JSON 解析与顶层类型错误的状态码和错误体与既有端点一致。

成功返回 200、`valid: true`，回显 `chart_id`、`asset_id`、`test_date`、`currency` 与两位小数的 `carrying_amount`/`recoverable_amount`，并附 `impairment_loss`、`post_impairment_carrying_amount` 与 `entry`。可收回金额低于账面金额时，差额（账面金额减可收回金额）为减值损失，减值后账面金额等于可收回金额；`entry` 沿用请求的 `voucher_id`、以测试日为 `posting_date`、币种取请求币种，`imp-1` 借记减值损失科目、`imp-2` 贷记累计减值科目，每行仅一侧大于零，借贷合计精确相等且可通过既有凭证校验。可收回金额不低于账面金额时损失为 `0.00`、减值后账面金额不变且 `entry` 为 `null`。金额使用精确十进制；处理不落盘、不保留状态，相同输入结果一致；既有公开方法与路由行为不变。

## 外币货币性项目期末重估

`POST /v1/foreign-currency-remeasurements/generate`（`Content-Type: application/json`）按结账日汇率无状态地重估外币货币性资产与负债头寸并生成本位币复式分录。请求含七个顶层字段，拒绝未知字段：`voucher_id` 为非空字符串；`remeasurement_date` 为真实 `YYYY-MM-DD` 日历日期；`currency` 为三位大写字母的本位币；`chart` 与科目体系校验同构（错误路径加 `/chart` 前缀），且 `effective_date` 不得晚于 `remeasurement_date`（`chart_not_effective`）；`fx_gain_account_code`、`fx_loss_account_code` 为非空字符串，两者相同时在 `/fx_loss_account_code` 报 `duplicate_fx_account`。`positions` 为非空数组（空数组报 `too_few_positions`），每项含唯一 `position_id`（重复报 `duplicate_position_id`）、`account_code`、`foreign_currency`、`foreign_amount`、`carrying_amount`、`exchange_rate`，头寸层同样拒绝未知字段。`foreign_amount` 须大于零、`carrying_amount` 可不小于零，均为至多两位小数的无符号十进制字符串，否则报 `invalid_amount`；`exchange_rate` 须大于零、至多八位小数且不用指数，否则报 `invalid_exchange_rate`。外币等于本位币时报 `functional_currency_position`；同一科目与外币组合重复时报 `duplicate_position`。头寸科目须为启用的 `asset` 或 `liability`，汇兑收益、损失科目分别须为启用的 `revenue`、`expense`，均按存在（`unknown_account`）、启用（`inactive_account`）、类别（`account_type_mismatch`）依次只报一个。关联字段无效时不派生错误：科目体系无效时不派生科目类错误，币种或头寸字段无效时不派生外币与组合重复错误。缺失、类型、空值、日期、币种错误沿用既有错误码；失败返回 422、`valid: false` 与按 `path`、`code` 排序的 `errors`；媒体类型、JSON 解析与顶层类型错误的状态码和错误体与既有端点一致。

成功返回 200、`valid: true`，回显 `chart_id`、`voucher_id`、`remeasurement_date`、`currency`，并按输入顺序给出各头寸的折算结果：每项含 `position_id`、`account_code`、`foreign_currency`、两位小数的 `foreign_amount`/`carrying_amount`、原样带回的 `exchange_rate`、`remeasured_amount`（外币金额乘汇率四舍五入至两位小数）、带符号两位小数 `adjustment`（折算额减账面金额）与借贷方向 `side`（资产增加记借、减少记贷，负债相反，零调整为 `null`）。全部非零调整按输入顺序进入同一 `entry`，账户行净额以汇兑收益科目贷记或汇兑损失科目借记抵平（净额为零时不加汇兑行），`line_id` 依次为 `fxr-1`、`fxr-2`……凭证沿用请求的 `voucher_id`、以重估日为 `posting_date`、币种取请求本位币，每行仅一侧大于零，借贷合计精确相等且可通过既有凭证校验。响应另附两位小数的 `net_fx_gain`、`net_fx_loss`；全部调整为零时二者均为 `0.00` 且 `entry` 为 `null`。金额使用精确十进制（`ROUND_HALF_UP`）；处理不落盘、不保留状态，相同输入结果一致；既有公开方法与路由行为不变。

## 合并财务报表生成

`POST /v1/consolidated-financial-statements/generate`（`Content-Type: application/json`）汇总多主体账务并抵消内部交易后，无状态地生成合并损益表与合并资产负债表。请求沿用财务报表端点的 `period_start`、`period_end`、`currency`、`chart`（校验与错误路径完全一致），新增 `entities` 与 `elimination_entries`，顶层拒绝未知字段。`entities` 为非空数组：非数组报 `invalid_type`，空数组报 `too_few_entities`，`entity_id` 重复时报 `duplicate_entity_id`（在后出现者路径报错）。每个主体含非空 `entity_id`、`opening_balances` 与 `entries`，主体层拒绝未知字段；全部主体共享期间、本位币和科目体系，其期初余额与凭证沿用试算平衡表端点的全部校验，错误路径加 `/entities/{i}` 前缀。`elimination_entries` 须为数组且可为空，每项沿用凭证校验，并须币种与请求一致（`currency_mismatch`）、`posting_date` 落在期间闭区间内（`posting_date_out_of_period`）、只引用已启用科目（`unknown_account`/`inactive_account`），错误路径加 `/elimination_entries/{i}` 前缀；抵消凭证只影响合并结果。依赖字段无效时不派生关联错误。失败返回 422、`valid: false` 与按 `path`、`code` 排序的 `errors`；媒体类型、JSON 解析与顶层类型错误的状态码和错误体与既有端点一致。

成功返回 200、`valid: true`，回显 `chart_id`、`period_start`、`period_end`、`currency` 并附整数 `entity_count`。合并口径为汇总各主体期初与本期发生额后，再把抵消分录计入合并本期发生额；`income_statement` 与 `balance_sheet` 的结构、金额方向、净利润计入权益、科目顺序、零值保留与 `balanced` 口径均与单账套财务报表端点一致。`elimination_summary` 按 `account_code` 字典序汇总抵消影响，每项含 `account_code` 与两位小数的 `debit`、`credit`。金额使用精确十进制并统一输出两位小数字符串；处理不落盘、不保留状态，相同输入结果一致；既有公开方法与路由行为不变。

## 递延所得税计算

`POST /deferred-tax/calculate`（`Content-Type: application/json`）根据报告日的账面价值与计税基础无状态地计算可追溯的递延所得税结果：只计算、不自动写入凭证，不改变既有报表、合并、期间结账与 HTTP 接口的既有语义。请求以单一记账本位币提交，含四个顶层字段（拒绝未知字段，`unknown_field`）：`reporting_date` 为真实 `YYYY-MM-DD` 日历日期；`currency` 为三位大写字母的记账本位币；`tax_rates` 为税率区间数组（可为空），每项含 `start_date`、`end_date`（均为真实日历日期且起始不晚于终止，否则 `invalid_period`）与 `rate`（0 到 1 之间的有限十进制值，否则 `invalid_tax_rate`），区间两两不得重叠（`overlapping_tax_rates`，在后出现者路径报错）；`items` 为暂时性差异项目数组（可为空）。每个项目含唯一 `item_id`（重复报 `duplicate_item_id`）、`classification`（`asset` 或 `liability`，否则 `invalid_classification`）、`carrying_amount`、`tax_base`、`expected_reversal_date` 与 `attribution`（`profit_or_loss`/`oci`/`equity`，否则 `invalid_attribution`）；金额只接受可无损转换为 Decimal 的有限十进制值（否则 `invalid_amount`）。资产项目的暂时性差异为账面价值减计税基础，负债项目为计税基础减账面价值；正数形成递延所得税负债，负数在可收回上限内形成递延所得税资产。可抵扣项目（差异为负）另须提供 `recoverable_cap`——本期有证据支持的可收回差异上限（缺失报 `required`），小于零报 `invalid_recoverable_cap`、超过可抵扣差异报 `recoverable_cap_exceeds_deductible`。`expected_reversal_date` 须恰好被一个税率区间覆盖：没有覆盖报 `no_applicable_tax_rate`，覆盖不止一个报 `ambiguous_tax_rate`。

校验失败时，公开 Python 入口 `ledgerlens.deferred_tax.calculate_deferred_tax`（及 `Service.calculate_deferred_tax`）统一抛出 `ValueError`（`DeferredTaxError` 子类，携带 `code` 与 `path`）；HTTP 入口返回 400 与 `{"error": {"code", "path", "message"}}`，`path` 以 JSON Pointer 定位到项目或税率区间，多个错误按 `path`、`code` 字典序只报告首个，不返回部分计算结果。成功返回 200：响应回显 `reporting_date`、`currency`，保留输入项目顺序逐项给出 `item_id`、`classification`、原始 `temporary_difference`、适用 `tax_rate`、`recognized_deductible_difference`、`unrecognized_deductible_difference`、`deferred_tax_asset`、`deferred_tax_liability` 与 `attribution`；每个项目的递延所得税按四舍五入（`ROUND_HALF_UP`）保留两位小数，`total_deferred_tax_asset`、`total_deferred_tax_liability`、`net_deferred_tax`（资产减负债）与按三种归属汇总的 `attribution_totals` 均由已舍入明细相加得到。空项目集合成功返回全零汇总。媒体类型不符返回 415 `unsupported_media_type`，JSON 解析失败返回 400 `invalid_json`，顶层非对象返回 400 `request_not_object`。处理不修改输入、不落盘、不保留状态，相同输入产生字段与值均一致的结果；既有公开方法与路由行为不变。

## 集团试算平衡表合并与内部交易抵消

公开 Python 入口 `ledgerlens.consolidate_group_trial_balance`（亦可经 `Service.consolidate_group_trial_balance` 调用）面向同一记账本位币、同一会计期间的母子公司余额数据，无状态地生成集团合并结果；本次不处理汇率折算、分步收购、处置与购买日公允价值调整。参数：`entities` 为实体余额序列，每项为 `EntityBalances` 或 `{"entity_id", "period", "balances"}` 字典，余额项为 `AccountBalance`、`{"account_code", "direction", "amount"}` 或现有试算平衡表产出的 `{"code"/"account_code", "debit"/"ending_debit", "credit"/"ending_credit"}` 行（仅一侧大于零，零额行忽略）；`account_mapping` 把各实体科目归并到统一集团科目；`ownership` 为实体标识到持股比例的映射（缺省视为 1 即全资）；`intercompany_pairs` 为成对内部往来声明，每对含双方实体、各自科目、各自声明金额、抵消类别（`receivable_payable`/`revenue_cost`/`dividend`）与可选业务引用；`tolerance` 为非负容差。系统只按声明抵消，不凭科目名称猜测交易关系。

校验依次执行：输入结构、容差非负与持股比例落在 0 到 1 闭区间（否则 `InvalidConsolidationInputError`）→ 实体期间一致（否则 `PeriodMismatchError`）→ 实体标识不重复（否则 `DuplicateEntityError`）→ 每份余额借贷平衡（否则 `UnbalancedEntityError`）→ 科目映射完整（否则 `AccountMappingError`）；任一失败即抛出异常，不返回部分合并结果。抵消规则：双方声明金额相等时全额抵消；差额绝对值不超过容差时以较小金额抵消并把差额写入对账差异；超过容差时保留双方原余额、不生成该对抵消分录并列为未匹配项目。

成功返回 `GroupConsolidationResult`：`group_balances` 按集团科目编码字典序给出抵消后净额（仅落一侧，零额两侧均为零）与 `total_debit`/`total_credit` 借贷合计；`elimination_entries` 逐笔给出类别、业务引用、抵消金额与可按实体和科目追溯的分录行；`reconciliation_differences` 与 `unmatched_items` 分别记录容差内差额与超容差未匹配项目；`minority_interests` 按子公司净资产余额（实体借贷平衡，取借方合计）乘以未持股比例计算非全资子公司的少数股东权益并单列，`minority_interest_total` 为其合计，不并入集团科目余额。返回集合按实体标识、科目编码与业务引用稳定排序；空实体集合返回金额全为零且明细为空的有效结果。全部金额运算保持 Decimal 精度，不转为二进制浮点数；处理不修改输入、不落盘、不保留跨调用状态，重复调用结果一致；既有报表、比率分析及其他公开功能的调用方式、异常和输出不变。

## 验证

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

当前基线刻意不包含复式记账、报表生成与审计留痕的实现，以便后续任务从已冻结事实出发独立设计并验证这些能力。
