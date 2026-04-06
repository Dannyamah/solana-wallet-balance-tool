"""
Solana historical balance client using Helius RPC.

Price + metadata pipeline
─────────────────────────
1. DexScreener (free, no key)
     → symbol, name, and *current* price for every traded SPL token.
       Used for metadata on all dates, and as price source when target_date is today.

2. CryptoCompare histoday (free, no key, 300 req/min)
     → end-of-day close price for a given historical date, keyed by ticker symbol.
       Covers SOL, BONK, USDC, USDT, JUP, RAY, WIF, mSOL, JTO, PYTH, and
       thousands of other tokens listed on major exchanges.

3. Helius DAS getAssetBatch (Helius key)
     → symbol / name fallback for tokens that DexScreener doesn't list.

4. CoinGecko simple/price (free, no key)
     → SOL price fallback if DexScreener and CryptoCompare both miss WSOL.
"""

import re
import time
import requests
from datetime import date, datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

WSOL_MINT           = "So11111111111111111111111111111111111111112"
DEXSCREENER_URL     = "https://api.dexscreener.com/latest/dex/tokens"
CRYPTOCOMPARE_URL   = "https://min-api.cryptocompare.com/data/v2/histoday"
COINGECKO_PRICE_URL = "https://api.coingecko.com/api/v3/simple/price"


class SolanaClientError(Exception):
    pass


class SolanaHistoricalClient:
    LAMPORTS_PER_SOL = 1_000_000_000
    TOKEN_PROGRAM_ID      = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
    TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

    def __init__(self, helius_api_key: str):
        self.api_key = helius_api_key
        self.rpc_url = f"https://mainnet.helius-rpc.com/?api-key={helius_api_key}"
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})
        self._last_rpc_call = 0.0
        self._min_rpc_interval = 0.11          # ~9 req/s — under free-tier 10 req/s

    # ──────────────────────────────────────────────────────────── internal helpers

    @staticmethod
    def is_valid_address(address: str) -> bool:
        return bool(re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", address or ""))

    def _throttle(self) -> None:
        wait = self._min_rpc_interval - (time.monotonic() - self._last_rpc_call)
        if wait > 0:
            time.sleep(wait)
        self._last_rpc_call = time.monotonic()

    def _rpc(self, method: str, params: list, retries: int = 4) -> object:
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        for attempt in range(retries):
            self._throttle()
            try:
                resp = self.session.post(self.rpc_url, json=payload, timeout=30)
                resp.raise_for_status()
                body = resp.json()
                if "error" in body:
                    err  = body["error"]
                    code = err.get("code", 0)
                    if code == 429 and attempt < retries - 1:
                        time.sleep(2 ** attempt)
                        continue
                    raise SolanaClientError(f"RPC error [{code}]: {err.get('message', err)}")
                return body.get("result")
            except requests.exceptions.RequestException as exc:
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                raise SolanaClientError(f"Network error: {exc}") from exc

    @staticmethod
    def _end_of_day_ts(d: date) -> int:
        return int(datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=timezone.utc).timestamp())

    @staticmethod
    def _account_keys(tx: dict) -> List[str]:
        raw = tx["transaction"]["message"]["accountKeys"]
        return [k["pubkey"] if isinstance(k, dict) else k for k in raw]

    # ──────────────────────────────────────────────────── signature / tx helpers

    def _last_sig_before(
        self, address: str, target_ts: int, max_pages: int = 30
    ) -> Tuple[Optional[str], Optional[int]]:
        """Most-recent transaction for *address* with blockTime <= target_ts."""
        before: Optional[str] = None
        for _ in range(max_pages):
            opts: Dict = {"limit": 1000, "commitment": "finalized"}
            if before:
                opts["before"] = before
            sigs = self._rpc("getSignaturesForAddress", [address, opts])
            if not sigs:
                return None, None
            for item in sigs:
                bt = item.get("blockTime")
                if bt is not None and bt <= target_ts:
                    return item["signature"], bt
            if len(sigs) < 1000:
                return None, None
            before = sigs[-1]["signature"]
        return None, None

    def _sol_balance_from_tx(self, signature: str, address: str) -> Optional[float]:
        tx = self._rpc(
            "getTransaction",
            [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
        )
        if not tx:
            return None
        try:
            keys = self._account_keys(tx)
            idx  = keys.index(address)
            return tx["meta"]["postBalances"][idx] / self.LAMPORTS_PER_SOL
        except (ValueError, KeyError, IndexError):
            return None

    def _token_amount_from_tx(self, signature: str, token_account: str) -> Optional[Dict]:
        tx = self._rpc(
            "getTransaction",
            [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
        )
        if not tx:
            return None
        try:
            keys = self._account_keys(tx)
            if token_account not in keys:
                return None
            idx = keys.index(token_account)
            for tb in tx["meta"].get("postTokenBalances", []):
                if tb["accountIndex"] == idx:
                    return tb["uiTokenAmount"]
            return {"uiAmount": 0.0, "decimals": 0}
        except (ValueError, KeyError, IndexError):
            return None

    # ──────────────────────────────────────────────────────────── public API

    def get_sol_balance_at_date(self, address: str, target_date: date) -> Dict:
        if target_date >= date.today():
            result = self._rpc("getBalance", [address, {"commitment": "finalized"}])
            bal = result["value"] / self.LAMPORTS_PER_SOL if result else None
            return {"balance": bal, "actual_tx_time": None, "is_current": True}

        target_ts = self._end_of_day_ts(target_date)
        sig, bt   = self._last_sig_before(address, target_ts)
        if sig is None:
            return {"balance": None, "actual_tx_time": None, "is_current": False}
        return {"balance": self._sol_balance_from_tx(sig, address),
                "actual_tx_time": bt, "is_current": False}

    def get_token_balances_at_date(
        self,
        address: str,
        target_date: date,
        progress_callback: Optional[Callable[[float, str], None]] = None,
    ) -> List[Dict]:
        """
        Returns list of:
          {mint, token_account, balance, decimals, symbol, name}
        Accounts created after target_date are omitted.
        """
        is_today  = target_date >= date.today()
        target_ts = self._end_of_day_ts(target_date)

        def _cb(pct: float, msg: str) -> None:
            if progress_callback:
                progress_callback(pct, msg)

        _cb(0.05, "Fetching token accounts…")

        token_accounts: List[Dict] = []
        for prog in (self.TOKEN_PROGRAM_ID, self.TOKEN_2022_PROGRAM_ID):
            result = self._rpc(
                "getTokenAccountsByOwner",
                [address, {"programId": prog}, {"encoding": "jsonParsed"}],
            )
            if result and "value" in result:
                token_accounts.extend(result["value"])

        if not token_accounts:
            return []

        results: List[Dict] = []
        total = len(token_accounts)

        for i, ta in enumerate(token_accounts):
            ta_pubkey: str = ta["pubkey"]
            info: Dict     = ta["account"]["data"]["parsed"]["info"]
            mint: str      = info["mint"]

            _cb(0.1 + (i / total) * 0.8, f"Checking token {i + 1}/{total}: {mint[:8]}…")

            if is_today:
                amt = info["tokenAmount"]
                results.append({
                    "mint": mint, "token_account": ta_pubkey,
                    "balance": amt.get("uiAmount") or 0.0,
                    "decimals": amt.get("decimals", 0),
                    "symbol": None, "name": None,
                })
            else:
                sig, _ = self._last_sig_before(ta_pubkey, target_ts)
                if sig is None:
                    continue
                amt = self._token_amount_from_tx(sig, ta_pubkey)
                if amt is None:
                    continue
                results.append({
                    "mint": mint, "token_account": ta_pubkey,
                    "balance": amt.get("uiAmount") or 0.0,
                    "decimals": amt.get("decimals", 0),
                    "symbol": None, "name": None,
                })

        _cb(0.93, "Fetching token metadata & prices…")
        return results

    # ──────────────────────────────────────────────── market data (price + metadata)

    def fetch_market_data(
        self, mints: List[str], target_date: date
    ) -> Dict[str, Dict]:
        """
        Return {mint: {"price": float, "symbol": str, "name": str,
                        "price_source": str, "price_available": bool}}

        For target_date == today  → current price from DexScreener.
        For historical dates      → end-of-day close from CryptoCompare histoday,
                                    using the ticker symbol obtained from DexScreener.
        """
        all_mints = list({WSOL_MINT} | set(mints))
        result: Dict[str, Dict] = {
            m: {"price": 0.0, "symbol": "", "name": "",
                "price_source": "", "price_available": False}
            for m in all_mints
        }
        is_today = target_date >= date.today()

        # ── Phase 1: DexScreener — symbol, name, and current price ────────────
        dex_found: set = set()

        for i in range(0, len(all_mints), 30):
            batch = all_mints[i : i + 30]
            try:
                resp = requests.get(
                    f"{DEXSCREENER_URL}/{','.join(batch)}",
                    timeout=15,
                    headers={"Accept": "application/json"},
                )
                resp.raise_for_status()
                pairs = [
                    p for p in (resp.json().get("pairs") or [])
                    if p.get("chainId") == "solana"
                ]

                by_base:  Dict[str, list] = {}
                by_quote: Dict[str, list] = {}
                for p in pairs:
                    b = (p.get("baseToken")  or {}).get("address", "")
                    q = (p.get("quoteToken") or {}).get("address", "")
                    if b:
                        by_base.setdefault(b, []).append(p)
                    if q and q in set(batch):
                        by_quote.setdefault(q, []).append(p)

                def _best(ps: list) -> dict:
                    return max(ps, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)

                for mint_addr, ps in by_base.items():
                    best  = _best(ps)
                    price = 0.0
                    try:
                        price = float(best.get("priceUsd") or 0)
                    except (ValueError, TypeError):
                        pass
                    sym  = (best.get("baseToken") or {}).get("symbol", "").strip()
                    name = (best.get("baseToken") or {}).get("name",   "").strip()
                    result[mint_addr].update(
                        symbol=sym, name=name,
                        price=price if is_today else 0.0,
                        price_source="DexScreener (live)" if is_today else "",
                        price_available=is_today and price > 0,
                    )
                    dex_found.add(mint_addr)

                for mint_addr, ps in by_quote.items():
                    if mint_addr in dex_found:
                        continue
                    best = _best(ps)
                    try:
                        base_usd     = float(best.get("priceUsd")    or 0)
                        price_native = float(best.get("priceNative") or 0)
                        price = base_usd / price_native if price_native else 0.0
                    except (ValueError, TypeError, ZeroDivisionError):
                        price = 0.0
                    sym  = (best.get("quoteToken") or {}).get("symbol", "").strip()
                    name = (best.get("quoteToken") or {}).get("name",   "").strip()
                    result[mint_addr].update(
                        symbol=sym, name=name,
                        price=price if is_today else 0.0,
                        price_source="DexScreener (live)" if is_today else "",
                        price_available=is_today and price > 0,
                    )
                    dex_found.add(mint_addr)
            except Exception:
                pass

        # ── Phase 2a: Today only — SOL CoinGecko fallback ─────────────────────
        if is_today and result.get(WSOL_MINT, {}).get("price", 0.0) == 0.0:
            try:
                r = requests.get(
                    COINGECKO_PRICE_URL,
                    params={"ids": "solana", "vs_currencies": "usd"},
                    timeout=10,
                )
                r.raise_for_status()
                sol_price = float(r.json().get("solana", {}).get("usd", 0))
                result[WSOL_MINT].update(
                    price=sol_price,
                    price_source="CoinGecko (live)",
                    price_available=sol_price > 0,
                )
                if not result[WSOL_MINT]["symbol"]:
                    result[WSOL_MINT]["symbol"] = "SOL"
                    result[WSOL_MINT]["name"]   = "Wrapped SOL"
            except Exception:
                pass

        # ── Phase 2b: Historical — CryptoCompare histoday end-of-day close ────
        if not is_today:
            # The daily candle for target_date has time = midnight UTC of that day.
            midnight_ts = int(
                datetime(
                    target_date.year, target_date.month, target_date.day,
                    tzinfo=timezone.utc,
                ).timestamp()
            )
            # toTs = start of next day ensures the target day's candle is the last returned.
            next_day_ts = midnight_ts + 86_400

            # Build symbol → [mints] map
            sym_to_mints: Dict[str, List[str]] = {}
            for mint_addr, info in result.items():
                sym = info.get("symbol", "").upper().strip()
                if sym:
                    sym_to_mints.setdefault(sym, []).append(mint_addr)

            for sym, sym_mints in sym_to_mints.items():
                try:
                    r = requests.get(
                        CRYPTOCOMPARE_URL,
                        params={"fsym": sym, "tsym": "USD",
                                "toTs": next_day_ts, "limit": 1},
                        timeout=12,
                    )
                    if r.status_code != 200:
                        continue
                    candles = r.json().get("Data", {}).get("Data", [])
                    # Match the candle whose open time == midnight of target_date
                    match = next(
                        (c for c in candles if c.get("time") == midnight_ts), None
                    )
                    if not match:
                        continue
                    close = float(match.get("close") or 0)
                    if close <= 0:
                        continue
                    for mint_addr in sym_mints:
                        result[mint_addr].update(
                            price=close,
                            price_source="CryptoCompare (EOD)",
                            price_available=True,
                        )
                    time.sleep(0.15)   # stay well under 300 req/min free limit
                except Exception:
                    pass

        # ── Phase 3: Helius DAS — metadata fallback for tokens not on DexScreener
        missing_meta = [m for m in all_mints if m not in dex_found]
        if missing_meta:
            for i in range(0, len(missing_meta), 100):
                batch = missing_meta[i : i + 100]
                try:
                    assets = self._rpc("getAssetBatch", [{"ids": batch}])
                    if not assets:
                        continue
                    for asset in assets:
                        if not asset or "id" not in asset:
                            continue
                        mid        = asset["id"]
                        token_info = asset.get("token_info") or {}
                        cmeta      = (asset.get("content") or {}).get("metadata") or {}
                        sym  = (token_info.get("symbol") or cmeta.get("symbol") or "").strip()
                        name = (cmeta.get("name") or token_info.get("name") or "").strip()
                        if sym:
                            result[mid]["symbol"] = sym
                        if name:
                            result[mid]["name"] = name
                except Exception:
                    pass

        return result
