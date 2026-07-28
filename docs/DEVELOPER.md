# 开发者文档

这个仓库使用跨境魔方开放API，从一组已确认的种子公司出发，扩展并筛选候选买家。

公开版本不包含平台密钥、真实客户名单、联系人、邮箱或原始海关数据。

## 使用合成数据重放

这一步不会调用真实API，也不会产生费用。

```bash
git clone https://github.com/raychen-sjtu/waimao-zhaoke.git
cd waimao-zhaoke

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python trade_snowball.py replay \
  --config examples/config.demo.json \
  --trace examples/replay_trace.json \
  --output-dir outputs/demo
```

输出包括：

- `leads.csv`
- `summary.json`
- `api_trace.json`
- `report.md`

## 使用跨境魔方API

先设置环境变量：

```bash
export TRADE_API_KEY="..."
export TRADE_API_BASE="https://openapi.upkuajing.com"

python trade_snowball.py run \
  --config examples/config.demo.json \
  --output-dir outputs/live
```

公开版本使用两个接口：

- `/search/company/list`：确认种子公司的完整实体；
- `/customs/company/list`：按已确认的供应商反查买家。

接口字段、价格和权限可能变化，请以自己的跨境魔方账号文档为准。

## 配置

配置文件中的主要字段：

- `seeds`：已知供应商及候选全名；
- `include_keywords`：目标产品的真实贸易语言；
- `exclude_keywords`：容易撞词的错误行业；
- `buyer_countries`：目标市场；
- `max_pages_per_seed`：单个种子的扩展上限；
- `qualified_threshold`和`review_threshold`：分档阈值。

真实项目不要把企业内部简称直接当作产品词。优先使用提单、产品页和采购方实际出现的语言。

## 结果解释

评分只用于排序和初筛，不是客户成交概率。

`qualified`仍需人工确认：

- 企业是真实买家、经销商还是货代；
- 产品与当前销售方向是否一致；
- 最近交易是否仍有业务意义；
- 联系人是不是采购决策人；
- 公开证据能否支持结论。

## 测试

```bash
python -m unittest discover -s tests
```

## 数据与隐私

不要提交：

- `.env`与API Key；
- 未经授权的客户、买家、供应商名单；
- 邮箱、电话、WhatsApp和个人社媒；
- 原始海关记录与付费API完整响应；
- 能够反推出委托方身份的种子企业组合。
