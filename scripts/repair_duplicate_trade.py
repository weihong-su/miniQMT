"""精确排除已确认的聚合/分笔重复流水；默认只预览，应用前创建一致性备份。"""
import argparse
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import sqlite3


def repair_duplicate(conn, keep_id, exclude_id, apply=False):
    """同事务验证身份、保留原行并写审计；不改持仓、现金或网格账本。"""
    if keep_id == exclude_id:
        raise ValueError("保留行和排除行不能相同")
    if apply:
        conn.execute("BEGIN IMMEDIATE")
    try:
        def load(ident):
            cursor=conn.execute("SELECT * FROM trade_records WHERE id=?",(ident,))
            row=cursor.fetchone()
            if row is None:
                raise ValueError("目标流水不存在")
            return dict(zip([column[0] for column in cursor.description],row))
        keep, duplicate=load(keep_id),load(exclude_id)
        for field in ("account","order_id","trade_type","fill_ids"):
            if not keep.get(field) or str(keep[field]) != str(duplicate.get(field)):
                raise ValueError("重复证据不一致: "+field)
        if str(keep["stock_code"]).split(".")[0] != str(duplicate["stock_code"]).split(".")[0]:
            raise ValueError("证券不一致")
        for field in ("price","volume","amount"):
            if keep[field] is None or abs(float(keep[field])-float(duplicate[field])) > 0.000001:
                raise ValueError("价量金额不一致: "+field)
        if (keep.get("strategy") != "grid" or duplicate.get("strategy") != "external"
                or str(keep["trade_id"]) != str(keep["order_id"])
                or str(duplicate["trade_id"]) != str(keep["fill_ids"])
                or keep.get("is_simulation") or duplicate.get("is_simulation")):
            raise ValueError("不属于已确认的实盘聚合/分笔重复类型")
        def day(row):
            return str(row.get("deal_time_str") or row.get("trade_time") or "")[:10]
        if len(day(keep)) != 10 or day(keep) != day(duplicate):
            raise ValueError("实际成交日期不一致")
        if keep.get("row_status","active") not in ("active",None):
            raise ValueError("保留行必须有效")
        state=duplicate.get("row_status") or "active"
        if state not in ("active","superseded"):
            raise ValueError("重复行状态不支持")
        result=dict(keep_id=keep_id,exclude_id=exclude_id,account=keep["account"],
                    order_id=keep["order_id"],trade_id=duplicate["trade_id"],
                    stock_code=keep["stock_code"],trade_date=day(keep),
                    price=keep["price"],volume=keep["volume"],amount=keep["amount"],
                    before=state,after="superseded",applied=False)
        if apply and state == "active":
            changed=conn.execute(
                "UPDATE trade_records SET row_status='superseded' WHERE id=? AND COALESCE(row_status,'active')='active'",
                (exclude_id,)).rowcount
            if changed != 1:
                raise RuntimeError("重复行状态已变化，拒绝继续")
            detail=dict(result,reason="实际成交编号已包含于保留的网格聚合行",batch=datetime.now().isoformat())
            conn.execute("""INSERT INTO run_events
                (account,event_time,level,event_type,code,order_id,trade_id,detail)
                VALUES (?,?,?,?,?,?,?,?)""",
                (keep["account"],datetime.now().strftime("%Y-%m-%d %H:%M:%S"),"INFO",
                 "duplicate_trade_superseded",keep["stock_code"],str(keep["order_id"]),
                 str(duplicate["trade_id"]),json.dumps(detail,ensure_ascii=False)))
            result["applied"]=True
        if apply:
            conn.commit()
        return result
    except Exception:
        if apply:
            conn.rollback()
        raise


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db",required=True,type=Path)
    parser.add_argument("--keep-id",required=True,type=int)
    parser.add_argument("--exclude-id",required=True,type=int)
    parser.add_argument("--apply",action="store_true")
    parser.add_argument("--backup-dir",type=Path)
    args=parser.parse_args()
    database=args.db.resolve()
    with closing(sqlite3.connect(database.as_uri()+"?mode=ro",uri=True)) as source:
        preview=repair_duplicate(source,args.keep_id,args.exclude_id)
        backup=None
        if args.apply:
            directory=args.backup_dir or database.parent
            directory.mkdir(parents=True,exist_ok=True)
            backup=directory/("before_duplicate_repair_"+datetime.now().strftime("%Y%m%d_%H%M%S")+".sqlite")
            with closing(sqlite3.connect(str(backup))) as target:
                source.backup(target)
                if target.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                    raise RuntimeError("备份校验失败，拒绝修正")
    if args.apply:
        with closing(sqlite3.connect(str(database),timeout=30)) as conn:
            preview=repair_duplicate(conn,args.keep_id,args.exclude_id,apply=True)
        preview["backup"]=str(backup)
    print(json.dumps(preview,ensure_ascii=False,indent=2))


if __name__ == "__main__":
    main()
