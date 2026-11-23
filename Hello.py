import asyncio
import inspect
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import agentscope
import gradio as gr
from agentscope.agent import Agent
from agentscope.credential import DashScopeCredential
from agentscope.message import UserMsg
from agentscope.model import DashScopeChatModel
from agentscope.permission import PermissionContext, PermissionMode
from agentscope.state import AgentState
from agentscope.tool import Toolkit, FunctionTool

# ==========================================
# 0. 基础配置
# ==========================================
BUILD = "tools-v2-2026-11-18"

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "my_money.db"

# API Key 只从环境变量或本地 key.txt 读，绝不写进代码（会被 GitHub 收录）
API_KEY = os.getenv("DASHSCOPE_API_KEY", "")
if not API_KEY:
    _key_file = BASE_DIR / "key.txt"
    if _key_file.exists():
        API_KEY = _key_file.read_text(encoding="utf-8").strip()

MODEL_NAME = "qwen-max"


def get_conn():
    return sqlite3.connect(DB_PATH)


# ==========================================
# 1. 分类规则（模糊归类）
# ==========================================
CATEGORIES = ["餐饮", "交通", "日用", "购物", "娱乐", "学习", "住房", "医疗", "通讯", "其他"]

# 关键词 -> 分类，命中任意一个就归到该分类
CATEGORY_KEYWORDS = {
    "餐饮": ["吃", "饭", "早餐", "午餐", "晚餐", "夜宵", "外卖", "奶茶", "咖啡", "火锅", "烧烤",
             "面", "粉", "水果", "零食", "食堂", "饮料", "黄焖鸡", "汉堡", "披萨", "蛋糕", "菜"],
    "交通": ["打车", "滴滴", "出租车", "地铁", "公交", "高铁", "火车", "动车", "机票", "飞机",
             "油费", "加油", "停车", "单车", "骑行", "车费", "过路费"],
    "日用": ["纸巾", "洗衣液", "牙膏", "洗发", "沐浴", "日用", "超市", "便利贴", "文具", "电池",
             "毛巾", "垃圾袋", "卫生纸"],
    "购物": ["衣服", "鞋", "包", "数码", "耳机", "手机", "电脑", "淘宝", "京东", "拼多多",
             "下单", "网购", "化妆品", "护肤"],
    "娱乐": ["电影", "游戏", "唱歌", "KTV", "旅游", "门票", "演出", "会员", "视频", "健身"],
    "学习": ["书", "课程", "网课", "打印", "资料", "考试", "学费", "培训", "文具"],
    "住房": ["房租", "水费", "电费", "物业", "宽带", "燃气", "宿舍"],
    "医疗": ["药", "医院", "挂号", "体检", "门诊", "看病", "口罩"],
    "通讯": ["话费", "流量", "手机费", "宽带费", "电话费"],
}

# 模型给的同义分类名 -> 标准分类
CATEGORY_ALIAS = {
    "交通出行": "交通", "出行": "交通", "差旅": "交通", "通勤": "交通",
    "食品": "餐饮", "吃饭": "餐饮", "饮食": "餐饮", "外卖餐饮": "餐饮",
    "日用百货": "日用", "生活用品": "日用", "日用品": "日用",
    "服饰": "购物", "数码": "购物", "网购": "购物",
    "娱乐休闲": "娱乐", "游玩": "娱乐",
    "教育": "学习", "学习用品": "学习",
    "房租水电": "住房", "居住": "住房",
    "看病": "医疗", "医药": "医疗",
    "电话": "通讯", "通信": "通讯",
}


def normalize_category(category: str, item: str) -> str:
    """把模型给的各种说法 / 商品名，归一到标准分类。"""
    raw = (category or "").strip()
    raw = CATEGORY_ALIAS.get(raw, raw)
    if raw in CATEGORIES:
        return raw

    text = f"{item}{raw}"
    for cat, keywords in CATEGORY_KEYWORDS.items():
        if any(k in text for k in keywords):
            return cat
    # 用户自定义分类（如"宠物"）就原样保留
    return raw if raw else "其他"


# ==========================================
# 2. 初始化数据库（含老库迁移）
# ==========================================
def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """CREATE TABLE IF NOT EXISTS expenses (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               item TEXT,
               amount REAL,
               category TEXT,
               created_at TEXT)"""
    )
    cur.execute(
        """CREATE TABLE IF NOT EXISTS budgets (
               category TEXT PRIMARY KEY,
               amount REAL)"""
    )

    # 老版本的表没有 created_at，补一列并给历史数据填上时间
    cols = [row[1] for row in cur.execute("PRAGMA table_info(expenses)").fetchall()]
    if "created_at" not in cols:
        cur.execute("ALTER TABLE expenses ADD COLUMN created_at TEXT")
        cur.execute(
            "UPDATE expenses SET created_at = datetime('now','localtime') WHERE created_at IS NULL"
        )

    # 把历史数据里不规范的分类名（如"日用品"）统一成标准分类
    for alias, canon in CATEGORY_ALIAS.items():
        cur.execute("UPDATE expenses SET category = ? WHERE category = ?", (canon, alias))

    conn.commit()
    conn.close()


init_db()


# ==========================================
# 3. 小工具函数
# ==========================================
def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _normalize_month(month: str) -> str:
    """""本"" / "上个月" / "11月" / "2026-11" / "2026年11月" -> "YYYY-MM"。"""
    now = datetime.now()
    m = (month or "").strip()
    if m in ("", "本月", "这个月", "当月", "这月"):
        return now.strftime("%Y-%m")
    if m in ("上月", "上个月", "上月同期"):
        return (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")

    m = m.replace("年", "-").replace("月", "").replace("/", "-").strip("-")
    parts = [p for p in m.split("-") if p]
    if len(parts) == 2 and len(parts[0]) == 4 and parts[1].isdigit():
        return f"{parts[0]}-{int(parts[1]):02d}"
    if len(parts) == 1:
        num = parts[0]
        if num.isdigit() and 1 <= int(num) <= 12:
            return f"{now.year}-{int(num):02d}"
        if len(num) == 6 and num.isdigit():
            return f"{num[:4]}-{num[4:]}"
    return now.strftime("%Y-%m")


def _month_spend(category: str, month: str) -> float:
    conn = get_conn()
    row = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM expenses "
        "WHERE category = ? AND strftime('%Y-%m', created_at) = ?",
        (category, month),
    ).fetchone()
    conn.close()
    return float(row[0] or 0)


def _budget_of(category: str):
    conn = get_conn()
    row = conn.execute("SELECT amount FROM budgets WHERE category = ?", (category,)).fetchone()
    conn.close()
    return float(row[0]) if row else None


def _budget_warning(category: str) -> str:
    """记完账顺手检查一下该分类本月预算。"""
    budget = _budget_of(category)
    if not budget:
        return ""
    spent = _month_spend(category, _normalize_month(""))
    if spent > budget:
        return f"\n⚠️ 超支提醒：本月【{category}】已花 {spent:.2f} 元，超出预算 {budget:.2f} 元，超 {spent - budget:.2f} 元。"
    if spent >= budget * 0.8:
        return f"\n提示：本月【{category}】已花 {spent:.2f} / {budget:.2f} 元，快到预算上限了。"
    return ""


# ==========================================
# 4. 工具函数
# ==========================================
def add_expense(item: str, amount: float, category: str = "") -> str:
    """
    记录一笔消费账单。
    参数:
        item: 消费的具体物品或事项，例如"打车"、"黄焖鸡"、"奶茶"
        amount: 消费金额，例如 18.5
        category: 消费分类，可留空由系统自动判断；标准分类为 餐饮、交通、日用、购物、娱乐、学习、住房、医疗、通讯、其他
    """
    try:
        cat = normalize_category(category, item)
        conn = get_conn()
        conn.execute(
            "INSERT INTO expenses (item, amount, category, created_at) VALUES (?, ?, ?, ?)",
            (item, float(amount), cat, _now_str()),
        )
        conn.commit()
        conn.close()
        return f"✅ 已记账：{item} {float(amount):.2f} 元，分类【{cat}】" + _budget_warning(cat)
    except Exception as e:
        return f"记账失败: {str(e)}"


def query_expenses(category: str = "", month: str = "") -> str:
    """
    查询某个分类的消费记录和总额，可按月份筛选。
    参数:
        category: 消费分类，例如"餐饮"；留空表示所有分类
        month: 月份，例如"2026-11"、"11月"、"本月"、"上个月"；留空表示全部时间
    """
    sql = "SELECT item, amount, created_at FROM expenses WHERE 1=1"
    args = []
    if category:
        cat = normalize_category(category, "")
        sql += " AND category = ?"
        args.append(cat)
    if month:
        sql += " AND strftime('%Y-%m', created_at) = ?"
        args.append(_normalize_month(month))
    sql += " ORDER BY created_at DESC"

    conn = get_conn()
    rows = conn.execute(sql, args).fetchall()
    conn.close()

    if not rows:
        return "没有查到符合条件的消费记录。"

    total = sum(r[1] for r in rows)
    detail = "；".join([f"{r[0]} {r[1]:.2f}元({(r[2] or '')[:10]})" for r in rows[:12]])
    more = f"（共 {len(rows)} 笔，只列最近 12 笔）" if len(rows) > 12 else ""
    scope = f"【{category or '全部'}】{('·' + month) if month else ''}"
    return f"📊 {scope}明细{more}：{detail}。合计 {total:.2f} 元。"


def monthly_summary(month: str = "") -> str:
    """
    按月份汇总消费：总支出 + 各分类占比。
    参数:
        month: 要汇总的月份，例如"2026-11"、"11月"、"本月"、"上个月"；留空表示本月
    """
    m = _normalize_month(month)
    conn = get_conn()
    rows = conn.execute(
        "SELECT category, SUM(amount), COUNT(*) FROM expenses "
        "WHERE strftime('%Y-%m', created_at) = ? GROUP BY category ORDER BY SUM(amount) DESC",
        (m,),
    ).fetchall()
    conn.close()

    if not rows:
        return f"{m} 还没有任何消费记录。"

    total = sum(r[1] for r in rows)
    lines = []
    for cat, amt, cnt in rows:
        pct = amt / total * 100 if total else 0
        lines.append(f"· {cat}：{amt:.2f} 元（{cnt} 笔，占 {pct:.1f}%）")
    return f"📅 {m} 共支出 {total:.2f} 元，分类明细：\n" + "\n".join(lines)


def list_recent(limit: int = 5) -> str:
    """
    列出最近几笔账单（带 id），用于查看或确认要删除哪一笔。
    参数:
        limit: 列出条数，默认 5
    """
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, item, amount, category, created_at FROM expenses ORDER BY id DESC LIMIT ?",
        (int(limit),),
    ).fetchall()
    conn.close()
    if not rows:
        return "还没有任何账单。"
    return "最近账单：\n" + "\n".join(
        [f"· id={r[0]} {r[1]} {r[2]:.2f}元【{r[3]}】{(r[4] or '')[:16]}" for r in rows]
    )


def delete_expense(expense_id: int = 0, item_keyword: str = "") -> str:
    """
    删除记错的一笔账。优先按 id 删除；没有 id 时按关键词删掉最近匹配的那一笔。
    参数:
        expense_id: 账单 id，可从 list_recent 得到；不确定就传 0
        item_keyword: 物品关键词，例如"打车"，在没给 id 时使用
    """
    conn = get_conn()
    try:
        if expense_id:
            row = conn.execute(
                "SELECT id, item, amount, category, created_at FROM expenses WHERE id = ?",
                (int(expense_id),),
            ).fetchone()
        elif item_keyword:
            row = conn.execute(
                "SELECT id, item, amount, category, created_at FROM expenses "
                "WHERE item LIKE ? ORDER BY id DESC LIMIT 1",
                (f"%{item_keyword}%",),
            ).fetchone()
        else:
            return "要删除哪一笔？请给出 id 或物品关键词。"

        if not row:
            return "没找到要删除的账单，先看看 list_recent 的结果。"

        conn.execute("DELETE FROM expenses WHERE id = ?", (row[0],))
        conn.commit()
        return (
            f"🗑️ 已删除：id={row[0]} {row[1]} {row[2]:.2f}元【{row[3]}】{(row[4] or '')[:16]}"
            f"（本月【{row[3]}】剩余支出 {_month_spend(row[3], _normalize_month('')):.2f} 元）"
        )
    finally:
        conn.close()


def set_budget(category: str, amount: float) -> str:
    """
    设置某个分类的本月预算（重复设置会覆盖）。
    参数:
        category: 分类，例如"餐饮"
        amount: 预算金额，例如 1000
    """
    cat = normalize_category(category, "")
    conn = get_conn()
    conn.execute(
        "INSERT INTO budgets (category, amount) VALUES (?, ?) "
        "ON CONFLICT(category) DO UPDATE SET amount = excluded.amount",
        (cat, float(amount)),
    )
    conn.commit()
    conn.close()
    return f"✅ 已设置【{cat}】月度预算 {float(amount):.2f} 元。"


def check_budget(category: str = "") -> str:
    """
    检查预算执行情况：本花了多少、还剩多少、是否超支。
    参数:
        category: 要检查的分类；留空表示检查所有已设置预算的分类
    """
    month = _normalize_month("")
    conn = get_conn()
    if category:
        cat = normalize_category(category, "")
        rows = conn.execute("SELECT category, amount FROM budgets WHERE category = ?", (cat,)).fetchall()
    else:
        rows = conn.execute("SELECT category, amount FROM budgets").fetchall()
    conn.close()

    if not rows:
        return "还没有设置任何预算，可以先用 set_budget 设置。"

    lines = []
    for cat, budget in rows:
        spent = _month_spend(cat, month)
        left = budget - spent
        status = "🔴 已超支" if left < 0 else ("🟡 接近上限" if left <= budget * 0.2 else "🟢 正常")
        lines.append(
            f"· {cat}：预算 {budget:.2f}，本月已花 {spent:.2f}，"
            f"{'超出' if left < 0 else '剩余'} {abs(left):.2f} 元 {status}"
        )
    return f"💰 {month} 预算执行情况：\n" + "\n".join(lines)


# ==========================================
# 5. 核心 Agent 逻辑
# ==========================================
SYSTEM_PROMPT = (
    "你是一个贴心的中文记账助手，通过工具完成记账、查询、汇总、删除和预算检查。\n"
    "工具使用规则：\n"
    "1. 用户说花了钱 -> 必须调用 add_expense；分类按语义判断，判断不了就留空，系统会自动归类。\n"
    "2. 用户问某类花销 -> query_expenses；问某个月/整月总账 -> monthly_summary。\n"
    "3. 用户说记错了要删 -> 先用 list_recent 确认，再 delete_expense 按 id 或关键词删除，并复述删掉的那笔。\n"
    "4. 用户提到预算、超支、还剩多少 -> set_budget / check_budget。\n"
    "5. 分类只用：餐饮、交通、日用、购物、娱乐、学习、住房、医疗、通讯、其他。\n"
    "6. 金额、分类、日期只能来自工具返回，绝不编造。回答简短、口语化，不要罗列工具名。"
)


def build_agent() -> Agent:
    """构建 Agent（只建一次，后面复用 -> 才有连续对话的上下文）。"""
    if not API_KEY:
        raise RuntimeError(
            "未找到 API Key：请设置环境变量 DASHSCOPE_API_KEY，"
            "或在项目目录放一个 key.txt（内容只有一行 key）"
        )

    toolkit = Toolkit(
        tools=[
            FunctionTool(add_expense),
            FunctionTool(query_expenses),
            FunctionTool(monthly_summary),
            FunctionTool(list_recent),
            FunctionTool(delete_expense),
            FunctionTool(set_budget),
            FunctionTool(check_budget),
        ]
    )

    kwargs = dict(
        name="记账小管家",
        system_prompt=SYSTEM_PROMPT,
        model=DashScopeChatModel(
            credential=DashScopeCredential(api_key=API_KEY),
            model=MODEL_NAME,
        ),
        toolkit=toolkit,
    )

    # 兼容两代 AgentScope 的权限参数
    params = inspect.signature(Agent.__init__).parameters
    if "state" in params:
        kwargs["state"] = AgentState(
            permission_context=PermissionContext(mode=PermissionMode.BYPASS)
        )
    if "require_tool_permission" in params:
        kwargs["require_tool_permission"] = False

    return Agent(**kwargs)


_agent = None


def get_agent() -> Agent:
    global _agent
    if _agent is None:
        _agent = build_agent()
    return _agent


async def chat_with_agent(user_query: str) -> str:
    reply_msg = await get_agent().reply(UserMsg(name="user", content=user_query))
    text = reply_msg.get_text_content()
    return text or str(reply_msg.content)


# ==========================================
# 6. Gradio 网页界面
# ==========================================
async def gradio_interface(user_query, history=None):
    try:
        return await chat_with_agent(user_query)
    except Exception as e:
        print(f"\n[后台真实报错] {type(e).__name__}: {e}")
        return f"⚠️ 运行出错: {type(e).__name__}: {e}"


demo = gr.ChatInterface(
    fn=gradio_interface,
    title="💰 基于 AgentScope 2.0 的智能记账助手",
    description=(
        "试试：'今天打车花了32块' / '这个月一共花了多少' / "
        "'把打车那笔删了' / '餐饮预算1000，看看超没超'"
    ),
)

if __name__ == "__main__":
    print("=" * 64)
    print(f"[启动] 代码版本   : {BUILD}")
    print(f"[启动] 正在运行   : {Path(__file__).resolve()}")
    print(f"[启动] Python     : {sys.executable}")
    print(f"[启动] agentscope : {agentscope.__version__}")
    print(f"[启动] 数据库     : {DB_PATH}")
    print("=" * 64)
    demo.launch()
