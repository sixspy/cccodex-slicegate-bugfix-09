
import hashlib, secrets, sqlite3
from datetime import datetime
from pathlib import Path
from fastapi import Header, HTTPException
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB_PATH = DATA / "app.db"
FRONTEND = ROOT / "frontend"
SESSIONS: dict[str, str] = {}

# ---------------------------------------------------------------------------
# 筛选一致说明（口径钉死，2026-11-17 起生效，当日禁止改口）
# 选定乙处置：筛选列表只作提示；放行写入前在写事务内现场重算完整度，
# 未完整则零写入并追加筛选偏离流水。可放行集合与放行写入同源裁决，
# 唯一裁决函数 adjudicate()，任何缓存列/前端断言一律不作放行依据。
# ---------------------------------------------------------------------------
POLICY = {
    "name": "筛选一致说明",
    "approach": "乙",
    "effective_date": "2026-11-17",
    "statement": "可放行集合与放行写入同源裁决：筛选列表只作提示，放行写前现场重算完整度；未完整则零写入并追加筛选偏离流水；存储的完整度缓存列与前端筛选断言一律不作放行依据。",
    "rule_complete": "完整 ⟺ expected_count > 0 且 row_count >= expected_count；row_count > 0 为部分完整；否则待检",
    "rule_releasable": "可放行 ⟺ 现场重裁为完整 且 release_status = 未放行",
    "contention": "同一筛选条件下抢点放行按写事务排队裁决，后手标注筛选争用",
    "deviation": "偏离本说明的放行请求记入筛选偏离流水并拒绝；筛选条件与切片不符记筛选口径偏了",
    "no_same_day_change": "口径当日禁止改口",
}


def adjudicate(row_count: int, expected_count: int) -> str:
    """唯一完整度裁决函数：列表、详情、闸口同源，不得另立口径。"""
    if expected_count > 0 and row_count >= expected_count:
        return "完整"
    return "部分完整" if row_count > 0 else "待检"


def releasable_of(row) -> tuple[bool, str]:
    """可放行集合的唯一判定：现场重裁完整 且 未放行。"""
    comp = adjudicate(row["row_count"], row["expected_count"])
    if comp != "完整":
        return False, f"现场重裁为{comp}，禁止放行"
    if row["release_status"] == "已放行":
        return False, "已放行，重复申请按筛选争用处理"
    return True, "完整且未放行"


def norm_filter(f: dict | None) -> dict:
    f = f or {}
    return {
        "biz_date": str(f.get("biz_date") or "").strip(),
        "partition_key": str(f.get("partition_key") or "").strip(),
        "completeness": str(f.get("completeness") or "").strip(),
        "releasable": bool(f.get("releasable")),
    }


def filter_key_of(f: dict) -> str:
    parts = [f"{k}={f[k]}" for k in ("biz_date", "partition_key", "completeness") if f.get(k)]
    if f.get("releasable"):
        parts.append("releasable=1")
    return "|".join(parts) or "(无筛选)"


def filter_mismatch(f: dict, row) -> str | None:
    """请求携带的筛选条件必须命中切片本身，且不得与现场重裁结论冲突。"""
    for k in ("biz_date", "partition_key"):
        if f.get(k) and f[k] != row[k]:
            return f"筛选{k}={f[k]}与切片实际{row[k]}不符"
    if f.get("completeness"):
        live = adjudicate(row["row_count"], row["expected_count"])
        if f["completeness"] != live:
            return f"筛选完整度={f['completeness']}与现场重裁{live}不符"
    return None


def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)  # 写锁等待：抢点放行排队而非报错
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None  # 显式事务：闸口用 BEGIN IMMEDIATE 串行裁决
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def sha(p: str) -> str:
    return hashlib.sha256(p.encode("utf-8")).hexdigest()


def ensure_users(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS users(
        username TEXT PRIMARY KEY, password_sha TEXT NOT NULL, role TEXT NOT NULL)""")
    if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("admin", sha("123456"), "管理员"))
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("viewer", sha("view123"), "只读"))


class LoginIn(BaseModel):
    username: str
    password: str


def require_user(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "未登录")
    u = SESSIONS.get(authorization.removeprefix("Bearer ").strip())
    if not u:
        raise HTTPException(401, "登录已失效")
    return u


from typing import Any
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

PRODUCT = "日切放行"
SKELETON = "业务日分区切片登记后裁决完整度，完整才放行下游，并与消费水位对账"

app = FastAPI(title=PRODUCT)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


def init_db():
    with connect() as conn:
        ensure_users(conn)
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS slices(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          slice_no TEXT UNIQUE NOT NULL,
          biz_date TEXT NOT NULL,
          partition_key TEXT NOT NULL,
          row_count INTEGER NOT NULL,
          expected_count INTEGER NOT NULL,
          completeness TEXT NOT NULL,
          release_status TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS releases(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          slice_id INTEGER NOT NULL,
          consumer TEXT NOT NULL,
          status TEXT NOT NULL,
          note TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS watermarks(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          consumer TEXT NOT NULL,
          biz_date TEXT NOT NULL,
          watermark TEXT NOT NULL,
          source_slice TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS deviations(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          slice_id INTEGER,
          slice_no TEXT NOT NULL,
          consumer TEXT NOT NULL,
          filter_key TEXT NOT NULL,
          kind TEXT NOT NULL,
          detail TEXT NOT NULL,
          actor TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        """)
        # 旧库补齐：releases 增加筛选口径与操作人列（单机 SQLite，就地迁移）
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(releases)")}
        if "filter_key" not in cols:
            conn.execute("ALTER TABLE releases ADD COLUMN filter_key TEXT NOT NULL DEFAULT ''")
        if "actor" not in cols:
            conn.execute("ALTER TABLE releases ADD COLUMN actor TEXT NOT NULL DEFAULT ''")
        if conn.execute("SELECT COUNT(*) c FROM slices").fetchone()["c"] == 0:
            rows = [
                ("SL-2026-11-01-east", "2026-11-01", "east", 980, 1000, "部分完整", "未放行"),
                ("SL-2026-11-01-west", "2026-11-01", "west", 1000, 1000, "完整", "已放行"),
                ("SL-2026-11-02-east", "2026-11-02", "east", 0, 1000, "待检", "未放行"),
            ]
            for r in rows:
                conn.execute("INSERT INTO slices(slice_no,biz_date,partition_key,row_count,expected_count,completeness,release_status) VALUES (?,?,?,?,?,?,?)", r)
            conn.execute("INSERT INTO releases(slice_id,consumer,status,note,created_at) VALUES (2,'ads_order','成功','完整切片放行','2026-11-01T18:00:00')")
            conn.execute("INSERT INTO watermarks(consumer,biz_date,watermark,source_slice) VALUES ('ads_order','2026-11-01','2026-11-01T18:00:00','SL-2026-11-01-west')")


@app.on_event("startup")
def _start():
    init_db()


@app.get("/api/health")
def health():
    with connect() as c:
        n = c.execute("SELECT COUNT(*) c FROM slices").fetchone()["c"]
    return {"ok": True, "product": PRODUCT, "skeleton": SKELETON, "slices": n, "db": str(DB_PATH)}


@app.post("/api/login")
def login(body: LoginIn):
    with connect() as c:
        row = c.execute("SELECT username, role FROM users WHERE username=? AND password_sha=?", (body.username, sha(body.password))).fetchone()
    if not row:
        raise HTTPException(401, "账号或密码错误")
    tok = secrets.token_hex(16); SESSIONS[tok] = row["username"]
    return {"token": tok, "username": row["username"], "role": row["role"]}


@app.get("/api/policy")
def get_policy(authorization: str | None = Header(default=None)):
    """策略条：筛选一致说明，全页面对齐可查；口径当日禁止改口。"""
    require_user(authorization)
    return POLICY


def slice_view(row) -> dict:
    """列表/详情共用的同源视图：完整度一律现场重裁，不读缓存列。"""
    d = dict(row)
    d["completeness"] = adjudicate(row["row_count"], row["expected_count"])
    ok, reason = releasable_of(row)
    d["releasable"] = ok
    d["releasable_reason"] = reason
    return d


@app.get("/api/slices")
def list_slices(biz_date: str = "", partition_key: str = "", completeness: str = "",
                releasable: str = "", authorization: str | None = Header(default=None)):
    require_user(authorization)
    sql, args = "SELECT * FROM slices WHERE 1=1", []
    if biz_date.strip():
        sql += " AND biz_date=?"; args.append(biz_date.strip())
    if partition_key.strip():
        sql += " AND partition_key=?"; args.append(partition_key.strip())
    sql += " ORDER BY id"
    with connect() as c:
        rows = c.execute(sql, args).fetchall()
    items = [slice_view(r) for r in rows]
    if completeness.strip():
        items = [i for i in items if i["completeness"] == completeness.strip()]
    if releasable.strip().lower() in ("1", "true", "是"):
        items = [i for i in items if i["releasable"]]
    f = norm_filter({"biz_date": biz_date, "partition_key": partition_key,
                     "completeness": completeness, "releasable": releasable.strip().lower() in ("1", "true", "是")})
    return {"items": items, "filter_key": filter_key_of(f)}


class SliceIn(BaseModel):
    slice_no: str
    biz_date: str
    partition_key: str
    row_count: int
    expected_count: int


@app.post("/api/slices")
def add_slice(body: SliceIn, authorization: str | None = Header(default=None)):
    require_user(authorization)
    comp = adjudicate(body.row_count, body.expected_count)
    with connect() as c:
        try:
            cur = c.execute(
                "INSERT INTO slices(slice_no,biz_date,partition_key,row_count,expected_count,completeness,release_status) VALUES (?,?,?,?,?,?,?)",
                (body.slice_no, body.biz_date, body.partition_key, body.row_count, body.expected_count, comp, "未放行"),
            )
            return {"id": cur.lastrowid, "completeness": comp}
        except sqlite3.IntegrityError:
            raise HTTPException(409, "切片号已存在")


@app.get("/api/slices/{sid}")
def slice_detail(sid: int, authorization: str | None = Header(default=None)):
    """切片详情：与筛选列表、放行闸同一套现场重裁口径。"""
    require_user(authorization)
    with connect() as c:
        row = c.execute("SELECT * FROM slices WHERE id=?", (sid,)).fetchone()
        if not row:
            raise HTTPException(404, "切片不存在")
        rel = c.execute("SELECT * FROM releases WHERE slice_id=? ORDER BY id DESC", (sid,)).fetchall()
        dev = c.execute("SELECT * FROM deviations WHERE slice_id=? ORDER BY id DESC", (sid,)).fetchall()
    return {"slice": slice_view(row), "releases": [dict(r) for r in rel], "deviations": [dict(r) for r in dev]}


@app.post("/api/slices/{sid}/evaluate")
def evaluate(sid: int, authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        row = c.execute("SELECT * FROM slices WHERE id=?", (sid,)).fetchone()
        if not row:
            raise HTTPException(404, "切片不存在")
        comp = adjudicate(row["row_count"], row["expected_count"])
        c.execute("UPDATE slices SET completeness=? WHERE id=?", (comp, sid))
    return {"id": sid, "completeness": comp}


class ReleaseIn(BaseModel):
    consumer: str
    filter: dict[str, Any] | None = None  # 值班当前筛选条件，用于口径对齐审计


@app.post("/api/slices/{sid}/release")
def release(sid: int, body: ReleaseIn, authorization: str | None = Header(default=None)):
    """放行闸（乙处置）：写事务内现场重算完整度，与筛选列表同源。

    - 筛选条件与切片不符      → 记 筛选口径偏了，拒绝，零写入；
    - 现场重裁未完整          → 记 筛选偏离，拒绝，零写入；
    - 已放行（抢点后手）      → 记 筛选争用，拒绝，零写入；
    - 完整且未放行            → 同事务写放行态+流水+水位。
    """
    user = require_user(authorization)
    now = datetime.now().isoformat(timespec="seconds")
    f = norm_filter(body.filter)
    fkey = filter_key_of(f)
    err: tuple[int, str] | None = None
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")  # 同一筛选条件下抢点放行在此排队
        row = c.execute("SELECT * FROM slices WHERE id=?", (sid,)).fetchone()
        if not row:
            raise HTTPException(404, "切片不存在")
        mism = filter_mismatch(f, row) if body.filter else None
        comp = adjudicate(row["row_count"], row["expected_count"])
        if mism:
            c.execute("INSERT INTO deviations(slice_id,slice_no,consumer,filter_key,kind,detail,actor,created_at) VALUES (?,?,?,?,?,?,?,?)",
                      (sid, row["slice_no"], body.consumer, fkey, "筛选口径偏了", mism, user, now))
            err = (409, f"筛选口径偏了：{mism}")
        elif comp != "完整":
            c.execute("INSERT INTO deviations(slice_id,slice_no,consumer,filter_key,kind,detail,actor,created_at) VALUES (?,?,?,?,?,?,?,?)",
                      (sid, row["slice_no"], body.consumer, fkey, "筛选偏离",
                       f"现场重裁为{comp}（实收{row['row_count']}/期望{row['expected_count']}），未完整零写入", user, now))
            err = (409, "未完整禁止放行：已记筛选偏离")
        elif row["release_status"] == "已放行":
            c.execute("INSERT INTO releases(slice_id,consumer,status,note,filter_key,actor,created_at) VALUES (?,?,?,?,?,?,?)",
                      (sid, body.consumer, "争用", "筛选争用：同一筛选条件下后到，切片已放行", fkey, user, now))
            err = (409, "筛选争用：切片已被放行，本次记为后手")
        else:
            c.execute("UPDATE slices SET release_status='已放行' WHERE id=?", (sid,))
            c.execute("INSERT INTO releases(slice_id,consumer,status,note,filter_key,actor,created_at) VALUES (?,?,?,?,?,?,?)",
                      (sid, body.consumer, "成功", "完整切片放行（现场重裁）", fkey, user, now))
            c.execute("INSERT INTO watermarks(consumer,biz_date,watermark,source_slice) VALUES (?,?,?,?)",
                      (body.consumer, row["biz_date"], now, row["slice_no"]))
        c.commit()
    if err:
        raise HTTPException(err[0], err[1])
    return {"ok": True}


@app.get("/api/releases")
def list_releases(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        rows = c.execute("""SELECT r.*, s.slice_no FROM releases r JOIN slices s ON s.id=r.slice_id ORDER BY r.id DESC""").fetchall()
    return {"items": [dict(r) for r in rows]}


@app.get("/api/deviations")
def list_deviations(authorization: str | None = Header(default=None)):
    """筛选偏离流水：闸口拒绝的每一次误放/口径偏移都可查。"""
    require_user(authorization)
    with connect() as c:
        rows = c.execute("SELECT * FROM deviations ORDER BY id DESC").fetchall()
    return {"items": [dict(r) for r in rows]}


@app.get("/api/watermarks")
def list_wm(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        rows = c.execute("SELECT * FROM watermarks ORDER BY id DESC").fetchall()
    return {"items": [dict(r) for r in rows]}


@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")


app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")
