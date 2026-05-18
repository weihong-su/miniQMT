"""一次性诊断脚本：测量 web 持仓刷新链路在两个账号上的实际表现。

不属于自动化测试集，仅用于人工诊断 — 完成调查后可删除。

测量两个维度：
  1) /api/positions-all 的 data_version 是否真的在按 tick 节奏递增
  2) /api/sse 的 positions_update.changed=true 推送频率
"""

import json
import time
import threading
import urllib.request
from collections import defaultdict


PORTS = [5000, 5001]
DURATION = 35  # 秒
SAMPLE_INTERVAL = 1.0  # 秒


def probe_positions_all(port, duration, samples):
    """每秒拉一次 /api/positions-all，记录 data_version 和现价。"""
    end = time.time() + duration
    while time.time() < end:
        t0 = time.time()
        try:
            url = f"http://127.0.0.1:{port}/api/positions-all?version=0"
            with urllib.request.urlopen(url, timeout=3) as r:
                d = json.loads(r.read())
                ver = d.get("data_version")
                items = d.get("data", []) or []
                prices = tuple(
                    (it.get("stock_code"), round(it.get("current_price") or 0, 3))
                    for it in items
                )
                samples.append((round(time.time() - t0, 3), ver, prices))
        except Exception as e:
            samples.append((None, None, f"ERROR: {e}"))
        time.sleep(SAMPLE_INTERVAL)


def probe_sse(port, duration, events):
    """连接 SSE，记录每次 positions_update 通知。"""
    end = time.time() + duration
    try:
        url = f"http://127.0.0.1:{port}/api/sse"
        req = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
        with urllib.request.urlopen(req, timeout=5) as r:
            buf = b""
            while time.time() < end:
                chunk = r.read1(4096) if hasattr(r, "read1") else r.read(1024)
                if not chunk:
                    break
                buf += chunk
                # SSE 事件由 \n\n 分隔
                while b"\n\n" in buf:
                    raw, buf = buf.split(b"\n\n", 1)
                    line = raw.decode("utf-8", errors="ignore").strip()
                    if not line.startswith("data: "):
                        continue
                    try:
                        payload = json.loads(line[6:])
                    except Exception:
                        continue
                    pu = payload.get("positions_update") or {}
                    events.append((round(time.time(), 3), pu.get("version"), pu.get("changed")))
    except Exception as e:
        events.append((round(time.time(), 3), None, f"ERROR: {e}"))


def summarize(port, samples, events):
    print(f"\n========= :{port} =========")
    # data_version 序列
    versions = [s[1] for s in samples if s[1] is not None]
    distinct = sorted(set(versions))
    deltas = [versions[i + 1] - versions[i] for i in range(len(versions) - 1)]
    pos_deltas = [d for d in deltas if d > 0]
    print(
        f"REST 采样: 共 {len(samples)} 次, version 范围 [{distinct[0] if distinct else '?'} .. {distinct[-1] if distinct else '?'}],"
        f" 不同版本数 = {len(distinct)}, 递增次数 = {len(pos_deltas)},"
        f" 单次递增中位值 ≈ {(sorted(pos_deltas)[len(pos_deltas)//2] if pos_deltas else 0)}"
    )

    # 把 1 秒采样序列里, 每次"版本变化的时间间隔"打出来 (用 index 近似秒数, 不精确但够用)
    change_secs = []
    last_v = None
    for i, s in enumerate(samples):
        v = s[1]
        if v is None:
            continue
        if last_v is None:
            last_v = v
            continue
        if v != last_v:
            change_secs.append(i)
            last_v = v
    if len(change_secs) >= 2:
        gaps = [change_secs[i + 1] - change_secs[i] for i in range(len(change_secs) - 1)]
        print(f"REST 版本号变化的相邻秒间隔: {gaps}  -> 平均 {sum(gaps)/len(gaps):.1f}s")
    else:
        print("REST 版本号变化次数过少, 无法算间隔")

    # SSE 通知
    sse_changed = [e for e in events if e[2] is True]
    sse_total = len(events)
    print(f"SSE 收到事件: {sse_total} 次, 其中 changed=true 的有 {len(sse_changed)} 次")
    if sse_changed:
        print(f"SSE changed=true 的 version 序列前 8: {[e[1] for e in sse_changed[:8]]}")
        ts = [e[0] for e in sse_changed]
        gaps = [round(ts[i+1] - ts[i], 1) for i in range(len(ts) - 1)]
        if gaps:
            print(f"SSE changed=true 推送间隔(秒): {gaps[:15]}  -> 平均 {sum(gaps)/len(gaps):.1f}s")

    # 现价是否在动
    if samples:
        # 看最后一次和第一次的 prices
        first_prices = next((s[2] for s in samples if isinstance(s[2], tuple)), None)
        last_prices = next((s[2] for s in reversed(samples) if isinstance(s[2], tuple)), None)
        if first_prices and last_prices:
            changed_codes = []
            d_first = dict(first_prices)
            d_last = dict(last_prices)
            for code, p0 in d_first.items():
                p1 = d_last.get(code)
                if p1 is not None and abs(p1 - p0) >= 0.005:
                    changed_codes.append((code, p0, p1))
            if changed_codes:
                print(f"采样期间真实价格发生变化: {changed_codes}")
            else:
                print("采样期间所有持仓现价无变化(<0.005)")


def main():
    threads = []
    rest_samples = defaultdict(list)
    sse_events = defaultdict(list)

    for port in PORTS:
        t1 = threading.Thread(target=probe_positions_all, args=(port, DURATION, rest_samples[port]))
        t2 = threading.Thread(target=probe_sse, args=(port, DURATION, sse_events[port]))
        t1.start(); t2.start()
        threads += [t1, t2]

    print(f"采样 {DURATION} 秒, 端口={PORTS}, 每秒一次 REST + 持续 SSE ...")
    for t in threads:
        t.join()

    for port in PORTS:
        summarize(port, rest_samples[port], sse_events[port])


if __name__ == "__main__":
    main()
