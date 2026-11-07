# srzone-v1

这是加在原支撑/阻力检测器外面的**新研究执行层**。GPL-3.0 与原项目相同。原 A 股检测、Web 界面和 `src/srlab` 评测没有改算法。

它**不是**：

- 原作者的下单策略。上游仓库只有关键位和 A 股走查标注，没有定仓、止损单或进出场。
- A 股 `data/score_calibration.json` / `data/prob_models.json` 的迁移。那些数字不能当成币圈已验证的胜率，也不能用高 hold 率推导收益。
- s2bt / strategy 2。
- 一份可以实盘的参数书。下面标成「研究假设」的数字是写死的纸面配置，没有搜索、没有按结果回改。

## 规则（新设计）

只在**已闭合** K 线上确认。信号棒的 high/low/close 此时都已知道，用来判断形态；**成交在下一根的开盘价**。不用信号棒的影线当成交价，也不用入场棒里尚未走出来的 high/low 去提前成交。

- 多头（`mode=long` 或 `both`）：上一根收盘在支撑区上沿之上，本根最低价打到上沿，本根收盘重新站上上沿，且本根最低价没有先打穿计划止损。
- 空头（`mode=short` 或 `both`）：上一根收盘在阻力区下沿之下，本根最高价打到下沿，本根收盘重新落到下沿之下，且没有先打穿计划止损。
- 同一根闭合 K 线同时给出多头和空头：两边都不做。
- 同一侧多个区：只留离现价最近的一个。
- 止损：多头在 `zone.low - sl_atr_buffer * ATR`，空头在 `zone.high + sl_atr_buffer * ATR`。ATR 用本仓库已有的因果滚动 ATR。
- 止盈：成交价 ± `reward_risk` × 实际止损距离。
- 时间退出：持有满 `max_hold_bars` 根闭合 K 线后，在**下一根开盘**市价平仓。
- 同一根里止损和止盈都打到：按止损成交。
- 跳空打穿止损：按更差的开盘价成交，不按止损价假装成交。
- 计划入场的那根开盘已经穿过止损：撤单，不成交。这里只用开盘价，不用那根的 high/low。
- 缺数据、NaN、历史短于 `min_bars`、检测器抛错、数量不满足步长或最小名义：不下单。
- 一个 symbol 同时只有一笔仓。平仓后 `cooldown_bars` 根内不再开仓。
- 费用 `fee_rate_per_side`、滑点 `slippage_rate`、资金费都是显式参数。资金费只在提供了该时刻的公开费率时入账；缺档记 `funding_gap`，不当成 0。

关键位来自现有 `V3Fusion` 的价格区（距离带与「现价不在区内」的过滤与 `SREngine.detect` 一致）。不读取守住概率。

## 执行与账户安全

纸面路径是 `PaperBroker` + `Supervisor`，研究回测走的就是这套状态机。

USD-M 实盘类 `BinanceUsdmLiveBroker` 只在下面**全部**成立时才签名请求，否则直接拒绝且不发 HTTP：

- `execution=live`
- `enable_live=true`
- `allow_private_api=true`
- `confirm_live` 等于 `I_AUTHORIZE_USD_M_LIVE_ORDERS`
- `confirm_symbol` 等于要交易的 symbol
- `capital_usdt`、`leverage`、`max_total_notional_usdt`、`risk_per_trade_frac`、`portfolio_loss_cap_frac`、费率、滑点都是正数且已填写
- 过滤器来自 `GET /fapi/v1/exchangeInfo`，`source=exchangeInfo`，且是 `TRADING` 的 USDT 本位永续

「补」这三个字不会打开实盘。研究脚本里的 ETHUSDT、1 倍、1 万 USDT 只是纸面样本，不能抄去当实盘授权。

适配器按公开文档组请求，本环境没有登录交易所，也没有读取密钥文件：

- 市价开仓、reduce-only 市价平仓：`POST /fapi/v1/order`
- 止损/止盈：`POST /fapi/v1/algoOrder`，`algoType=CONDITIONAL`，`STOP_MARKET` / `TAKE_PROFIT_MARKET`。Binance 2025-11-06 变更日志写明，2025-12-09 起这些条件单走 Algo Service，再发到 `POST /fapi/v1/order` 会得到 `-4120`
- 保护单带 `reduceOnly=true` 和**本笔成交数量**。不传 `closePosition`，避免把别人的仓一起平掉
- 撤单只撤 `srz` 前缀且属于本账本的 `clientAlgoId` / `origClientOrderId`
- 不调用 `DELETE /fapi/v1/allOpenOrders`、`DELETE /fapi/v1/algoOpenOrders`、`POST /fapi/v1/countdownCancelAll`
- 提交超时：先按 client id 查询。查无此单就停止开仓，不换一个新 id 重发。查到了就沿用原单
- 部分成交：保护单数量等于已成交数量
- 保护单失败：立刻 reduce-only 平掉本策略数量。平仓也失败则写入持久 `risk_halt`，重启后仍然有效，直到显式 `CLEAR_RISK_HALT`
- 交易所上有账本对不上的仓（外部仓、对冲模式）：停止开仓，不接管，不撤别人的单
- 私有接口失败：停止开仓
- 权益触及 `portfolio_loss_cap_frac`：持久风险停止，不再开新仓；已有保护单仍保留

账本是 JSON，原子替换写入。重启加载账本，而不是把交易所仓位自动记成自己的。

## 已实现

- 因果信号、纸面撮合、费用、滑点、资金费入账、一仓、冷却、时间退出
- 上述账户安全状态机，以及对应单元测试（`tests/test_srzone.py`）
- 公开历史文件读取：`https://data.binance.vision` 的 USD-M 月度 K 线与资金费，并用官方 `.CHECKSUM` 做 sha256
- 一个冻结参数的 ETHUSDT 1h 研究回测脚本：`scripts/srzone_research_backtest.py`

## 未验证

- 没有实盘、没有测试网下单、没有读取用户 API key
- `fapi.binance.com` 在编写环境返回 **451**（地区限制），不是 200。因此没有拉到真实 `exchangeInfo`。研究回测里的 tick/step/minQty/minNotional 是脚本里的**假设**，`source=NOT_LIVE_EXCHANGE_INFO`，不能当成交易所过滤器已核对
- 5 bp 单边费用和 1 bp 滑点是假设，不是账户的 `commissionRate`
- 资金费用合约 K 线开盘价近似标记价格
- A 股概率模型没有在币圈样本外验证
- 没有做参数搜索。样本结果不是优化出来的绩效承诺
- 实盘循环（用户数据流、持续守护进程）没有部署。`BinanceUsdmLiveBroker` 只是被门禁挡住的请求适配器

## 部署前必须由账户所有人自己确定

缺任何一项都不应启动实盘：

| 项 | 原因 |
| --- | --- |
| symbol | 研究脚本用 ETHUSDT 只是因为公开数据好拿，不是选币结论 |
| 现货还是 U 本位永续 | 本适配器只写了 USD-M USDT 永续 |
| `capital_usdt` | 没有默认实盘资金 |
| `leverage` | 纸面研究是 1，不是 10 |
| `max_total_notional_usdt` | 总名义上限 |
| `risk_per_trade_frac` | 单笔风险占权益 |
| `portfolio_loss_cap_frac` | 组合亏损停止线 |
| `fee_rate_per_side` / `slippage_rate` | 必须按真实费率填写 |
| 当日 `exchangeInfo` 过滤器 | 必须来自交易接口，不能用研究假设 |
| `confirm_live` 与 `confirm_symbol` | 两个独立确认 |
| 单向持仓 | 对冲模式会直接停止开仓 |

原 `pa-agent-eth` 不应被本包重启。本包也不读取、不迁移其他交易所的授权。

## 复现研究回测

```bash
python3 -m unittest tests.test_srzone
python3 scripts/srzone_research_backtest.py
```

报告写到 `research_out/srzone_v1_ethusdt_1h_2024H1.json`。窗口在看结果之前就定死：

- 基线：信号时间 2024-01-01 ≤ t < 2024-04-01，数据从 2023-11 起做预热
- 留出：信号时间 2024-04-01 ≤ t < 2024-07-01，数据从 2024-02 起做预热
- 周期 1h，`mode=both`，`sl_atr_buffer=0.25`，`reward_risk=1.5`，`max_hold_bars=24`，`cooldown_bars=6`
- 纸面资金 10000 USDT，杠杆 1，单笔风险 0.5%，名义上限 10000，亏损停止 20%

无密钥源码包可以用 `git archive` 从该提交打出，不包含 API key。
