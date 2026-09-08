"""처리 흐름 — DESIGN.md 6절.

1. T 결정. 휴장일이면 로그 남기고 종료 (exit 3)
2. 시장별 유니버스 로드 → 제외 규칙 적용 (시장당 1회)
3. for market × period: 구간 계산 → 지수 → 전종목 등락률 → inner join → 초과수익률 → 필터 → 상위/하위 N
4. 20개 랭킹을 long-format 하나로 통합
5. outputs/<T>/movers.csv, movers.md 저장 + SQLite upsert (+ 알림은 6단계)

종료코드: 0 전부 성공 / 1 일부·전부 실패(KRX 접근 불가 포함, 실패 목록·원인 분류 보고) / 2 자격증명 없음 / 3 휴장일
한 조합이 실패해도 나머지 조합은 계속 진행하고, 성공한 조합의 결과는 저장한다.

실행: python -m src.main [--base-date YYYYMMDD] [--markets KOSPI,KOSDAQ] [--periods 1d,1w] [--top-n N]
                          [--config config.yaml] [--cache-dir DIR] [--output-dir DIR] [--sqlite-path FILE] [--no-db]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

import pandas as pd
import yaml

from src.calc import RESULT_COLUMNS, compute
from src.calendar import period_window, resolve_base_date
from src.fetch import Fetcher, KRXCredentialsError, KRXUnavailableError
from src.rank import top_and_bottom
from src.report import render_markdown, upsert_sqlite, write_csv, write_markdown
from src.universe import build_universe

log = logging.getLogger(__name__)

EXIT_OK, EXIT_FAILED, EXIT_CREDENTIALS, EXIT_HOLIDAY = 0, 1, 2, 3
ROOT = Path(__file__).resolve().parent.parent


# ──────────────────────────────────────────────────────────────────────────
# 결과 구조
# ──────────────────────────────────────────────────────────────────────────
@dataclass
class ComboResult:
    market: str
    period: str
    ok: bool = False
    window: dict | None = None            # {ref_date, from, base_date}
    ranked: pd.DataFrame | None = None    # long format (up N + down N)
    n_calc_rows: int = 0
    market_ret: float | None = None
    inputs: dict = field(default_factory=dict)   # n_price_change, n_delisted_rows, index_rows, n_market_cap
    excess_stats: dict | None = None
    error: str | None = None
    error_class: str | None = None
    timings: dict = field(default_factory=dict)  # {단계: 초}

    def as_dict(self) -> dict:
        return {"market": self.market, "period": self.period, "ok": self.ok, "window": self.window,
                "from": (self.window or {}).get("from"), "base_date": (self.window or {}).get("base_date"),
                "n_calc_rows": self.n_calc_rows, "market_ret": self.market_ret, "inputs": self.inputs,
                "excess_stats": self.excess_stats, "error": self.error, "error_class": self.error_class,
                "timings": self.timings, "n_ranked": int(len(self.ranked)) if self.ranked is not None else 0}


@dataclass
class RunResult:
    base_date: str
    T: str | None
    combos: list[ComboResult] = field(default_factory=list)
    universes: dict = field(default_factory=dict)      # {market: {steps, warnings}}
    warnings: list[str] = field(default_factory=list)
    failure: dict | None = None                        # KRX 접근 불가 진단 (T 조차 못 구한 경우)
    note: str | None = None
    timings: dict = field(default_factory=dict)        # 공통 단계 {라벨: 초}
    fetch_stats: dict = field(default_factory=dict)
    top_n: int = 0
    total_sec: float = 0.0

    @property
    def movers(self) -> pd.DataFrame:
        frames = [c.ranked for c in self.combos if c.ok and c.ranked is not None]
        if not frames:
            return pd.DataFrame(columns=RESULT_COLUMNS)
        return pd.concat(frames, ignore_index=True)[RESULT_COLUMNS]

    @property
    def failures(self) -> list[ComboResult]:
        return [c for c in self.combos if not c.ok]

    @property
    def exit_code(self) -> int:
        if self.T is None:
            return EXIT_FAILED if self.failure else EXIT_HOLIDAY
        return EXIT_OK if self.combos and not self.failures else EXIT_FAILED

    def as_dict(self) -> dict:
        return {"base_date": self.base_date, "T": self.T, "note": self.note, "top_n": self.top_n,
                "exit_code": self.exit_code, "total_sec": self.total_sec,
                "combos": [c.as_dict() for c in self.combos],
                "failures": [{"market": c.market, "period": c.period, "error_class": c.error_class, "error": c.error}
                             for c in self.failures],
                "universes": self.universes, "warnings": self.warnings, "failure": self.failure,
                "timings": self.timings, "fetch_stats": self.fetch_stats,
                "n_movers_rows": int(len(self.movers))}


# ──────────────────────────────────────────────────────────────────────────
# 유틸
# ──────────────────────────────────────────────────────────────────────────
def load_config(path: str | Path = ROOT / "config.yaml") -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def _timed(timings: dict, label: str, fn: Callable, *args, **kwargs):
    t0 = time.perf_counter()
    try:
        return fn(*args, **kwargs)
    finally:
        timings[label] = round(time.perf_counter() - t0, 2)


def _error_class(e: BaseException) -> str:
    if isinstance(e, KRXUnavailableError):
        return f"krx:{e.classification}"
    return type(e).__name__


# ──────────────────────────────────────────────────────────────────────────
# 단계
# ──────────────────────────────────────────────────────────────────────────
def prepare_universe(fetcher: Fetcher, config: Mapping, T: str, market: str, *, etf, etn, timings: dict):
    """시장별 유니버스 (시장당 1회). 반환 UniverseResult."""
    listed = _timed(timings, f"{market}/listed", fetcher.listed, T, market)
    admin = _timed(timings, f"{market}/administrative", fetcher.administrative_tickers, T, market, listed)
    return build_universe(listed, market, config, etf_tickers=etf, etn_tickers=etn, administrative_tickers=admin)


def run_combo(fetcher: Fetcher, config: Mapping, T: str, market: str, period: str, universe_df: pd.DataFrame,
              top_n: int, market_cap: pd.DataFrame | None = None) -> ComboResult:
    """한 (시장, 기간) 조합. 예외는 잡아서 ComboResult.error 로 돌려준다 (나머지 조합 계속 진행)."""
    res = ComboResult(market=market, period=period)
    tm = res.timings
    try:
        w = _timed(tm, "window", period_window, period, T, config["periods"][period], fetcher.nearest)
        res.window = {"ref_date": w.ref_date, "from": w.from_date, "base_date": w.base_date}
        idx = _timed(tm, "index_ohlcv", fetcher.index_ohlcv, w.base_date, T, market)
        pc = _timed(tm, "price_change", fetcher.price_change, w.from_date, T, market)
        if market_cap is None:
            market_cap = _timed(tm, "market_cap", fetcher.market_cap, T, market)
        res.inputs = {"n_price_change": int(len(pc)),
                      "n_delisted_rows": int(((pc["종가"] == 0) & (pc["등락률"] == -100)).sum()),
                      "index_rows": int(len(idx)), "n_market_cap": int(len(market_cap))}
        calc = _timed(tm, "calc", compute, market, w, price_change=pc, index_df=idx, universe=universe_df,
                      market_cap=market_cap, config=config)
        res.n_calc_rows = int(len(calc))
        res.market_ret = float(calc["market_ret"].iloc[0]) if len(calc) else None
        res.excess_stats = ({k: round(float(v), 3) for k, v in calc["excess_ret"].describe().items()} if len(calc) else None)
        res.ranked = _timed(tm, "rank", top_and_bottom, calc, top_n)
        res.ok = True
        log.info("[%s/%s] ok: from=%s base=%s market_ret=%s rows=%d", market, period, w.from_date, w.base_date,
                 f"{res.market_ret:+.3f}%" if res.market_ret is not None else "-", res.n_calc_rows)
    except Exception as e:  # noqa: BLE001 — 한 조합 실패는 기록하고 계속
        res.error, res.error_class = f"{type(e).__name__}: {str(e)[:500]}", _error_class(e)
        log.error("[%s/%s] FAILED %s", market, period, res.error)
    return res


def run(config: Mapping, fetcher: Fetcher, base_date: str, *, markets: list[str] | None = None,
        periods: list[str] | None = None, top_n: int | None = None) -> RunResult:
    """DESIGN.md 6절 전체 흐름 (출력 제외). KRXCredentialsError 는 호출자에게 전파한다 (exit 2)."""
    t0 = time.perf_counter()
    markets = list(markets or config["markets"])
    periods = list(periods or config["periods"])
    top_n = int(top_n or config.get("top_n", 20))
    result = RunResult(base_date=base_date, T=None, top_n=top_n)
    tm = result.timings
    try:
        T = _timed(tm, "resolve_base_date", resolve_base_date, base_date, fetcher.nearest)
    except KRXUnavailableError as e:
        result.failure = {"classification": e.classification, **e.diagnosis, "error": str(e)}
        result.warnings.append(f"KRX 접근 불가 [{e.classification}]: {str(e)[:300]}")
        log.error("KRX 접근 불가 [%s]: %s", e.classification, e)
        result.fetch_stats, result.total_sec = dict(fetcher.stats), round(time.perf_counter() - t0, 1)
        return result
    if T is None:
        result.note = f"{base_date} 는 휴장일 — 종료 (exit {EXIT_HOLIDAY})"
        log.info(result.note)
        result.fetch_stats, result.total_sec = dict(fetcher.stats), round(time.perf_counter() - t0, 1)
        return result
    result.T = T

    # 공통 목록 (ETF/ETN) — 실패하면 유니버스를 만들 수 없으므로 전 조합 실패 처리
    try:
        etf = _timed(tm, "etf_tickers", fetcher.etf_tickers, T)
        etn = _timed(tm, "etn_tickers", fetcher.etn_tickers, T)
    except Exception as e:  # noqa: BLE001
        msg = f"{type(e).__name__}: {str(e)[:500]}"
        for m in markets:
            for p in periods:
                result.combos.append(ComboResult(m, p, error=f"ETF/ETN 목록 실패 → {msg}", error_class=_error_class(e)))
        result.fetch_stats, result.total_sec = dict(fetcher.stats), round(time.perf_counter() - t0, 1)
        return result

    for market in markets:
        try:
            uni = prepare_universe(fetcher, config, T, market, etf=etf, etn=etn, timings=tm)
            result.universes[market] = {"steps": uni.steps, "warnings": uni.warnings}
            result.warnings.extend(uni.warnings)
            market_cap = _timed(tm, f"{market}/market_cap", fetcher.market_cap, T, market)
        except Exception as e:  # noqa: BLE001 — 시장 준비 실패 → 그 시장의 전 기간 실패, 다른 시장은 계속
            msg = f"{type(e).__name__}: {str(e)[:500]}"
            for p in periods:
                result.combos.append(ComboResult(market, p, error=f"유니버스/시총 준비 실패 → {msg}", error_class=_error_class(e)))
            log.error("[%s] 유니버스 준비 실패: %s", market, msg)
            continue
        for period in periods:
            result.combos.append(run_combo(fetcher, config, T, market, period, uni.df, top_n, market_cap=market_cap))

    result.fetch_stats, result.total_sec = dict(fetcher.stats), round(time.perf_counter() - t0, 1)
    return result


def write_outputs(result: RunResult, config: Mapping, *, output_dir: str | Path | None = None,
                  sqlite_path: str | Path | None = None, formats: list[str] | None = None,
                  write_db: bool = True) -> dict:
    """outputs/<T>/movers.csv, movers.md, SQLite upsert. 반환: 생성 경로."""
    if result.T is None:
        return {}
    ocfg = dict(config.get("output", {}) or {})
    out_dir = Path(output_dir or ocfg.get("dir", "outputs")) / result.T
    formats = list(formats or ocfg.get("formats", ["csv", "md"]))
    movers = result.movers
    paths: dict = {}
    if "csv" in formats:
        paths["csv"] = str(write_csv(movers, out_dir / "movers.csv"))
    if "md" in formats:
        md = render_markdown(movers, T=result.T, combos=[c.as_dict() for c in result.combos], warnings=result.warnings,
                             failures=[{"market": c.market, "period": c.period, "error_class": c.error_class, "error": c.error}
                                       for c in result.failures], top_n=result.top_n)
        paths["md"] = str(write_markdown(md, out_dir / "movers.md"))
    if write_db and len(movers):
        db = Path(sqlite_path or ocfg.get("sqlite_path", "data/movers.db"))
        paths["sqlite"] = str(db)
        paths["sqlite_rows"] = upsert_sqlite(movers, db)
    return paths


# ──────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="relative-movers — 시장 대비 상대 등락 종목 일별 산출")
    ap.add_argument("--base-date", default=dt.date.today().strftime("%Y%m%d"), help="실행일 YYYYMMDD (기본 오늘)")
    ap.add_argument("--markets", default=None, help="쉼표 구분 (기본 config.markets)")
    ap.add_argument("--periods", default=None, help="쉼표 구분 (기본 config.periods)")
    ap.add_argument("--top-n", type=int, default=None)
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--cache-dir", default=None)
    ap.add_argument("--output-dir", default=None)
    ap.add_argument("--sqlite-path", default=None)
    ap.add_argument("--no-db", action="store_true", help="SQLite 저장 생략")
    ap.add_argument("--result-json", default=None, help="실행 요약 JSON 경로 (검증 워크플로용)")
    return ap


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    config = load_config(args.config)
    markets = args.markets.split(",") if args.markets else None
    periods = args.periods.split(",") if args.periods else None
    fetcher = Fetcher(config, cache_dir=args.cache_dir)
    try:
        result = run(config, fetcher, args.base_date, markets=markets, periods=periods, top_n=args.top_n)
    except KRXCredentialsError as e:
        print(str(e), file=sys.stderr)
        return EXIT_CREDENTIALS

    paths = write_outputs(result, config, output_dir=args.output_dir, sqlite_path=args.sqlite_path, write_db=not args.no_db)
    summary = result.as_dict()
    summary["paths"] = paths
    summary["run_at"] = dt.datetime.now().isoformat(timespec="seconds")
    if args.result_json:
        p = Path(args.result_json)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    # 최종 보고
    if result.T is None:
        print(result.note or f"KRX 접근 불가: {(result.failure or {}).get('error')}")
    else:
        ok = [c for c in result.combos if c.ok]
        print(f"\nT={result.T} · {len(ok)}/{len(result.combos)} 조합 성공 · movers rows={len(result.movers)} · "
              f"{result.total_sec}s · fetch {result.fetch_stats}")
        for k, v in paths.items():
            print(f"  {k}: {v}")
        for w in result.warnings:
            print(f"  ⚠️ {w}")
        if result.failures:
            print("\n실패 목록:")
            for c in result.failures:
                print(f"  ❌ {c.market} {c.period}: [{c.error_class}] {c.error}")
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
