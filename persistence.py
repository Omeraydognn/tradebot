"""
Kalıcılık (persistence) katmanı.

Sistem yeniden başladığında portföyler, işlem geçmişi ve AI ajanlarının
öğrendiği parametreler kaybolmasın diye periyodik olarak diske yazar ve
açılışta geri yükler.

Basit ve dayanıklı: tek JSON dosyası + atomik yazma (önce .tmp, sonra rename).
Bozuk/eksik dosya durumunda sessizce temiz başlar (asla çökmez).
"""
import json
import os
import time
import logging
from typing import Optional

logger = logging.getLogger(__name__)

STATE_FILE = os.getenv("STATE_FILE", "state.json")
SAVE_INTERVAL_SEC = float(os.getenv("SAVE_INTERVAL_SEC", "30"))

# Kaç işlem saklansın. "Her pencereye gir" modunda bot başına günde ~288,
# 7 botla ~2000 işlem üretilir; 200'lük eski tavan bir günü bile tutmuyordu
# ve arayüzde geçmiş "birkaç tane" görünüyordu.
MAX_SAVED_TRADES = int(os.getenv("MAX_SAVED_TRADES", "5000"))

# ==================================================================== #
#  DEPOLAMA KATMANI — DEPLOY'DAN SAĞ ÇIKMAK İÇİN                       #
#                                                                       #
#  SORUN: Render'ın (ve çoğu PaaS'ın) dosya sistemi EPHEMERAL'dır.      #
#  Her deploy'da ve free planda her uyku/uyanma çevriminde konteyner    #
#  sıfırdan kurulur; çalışma anında yazılan state.json SİLİNİR. Yani    #
#  bakiyeler, işlem geçmişi, kalibratör örnekleri, ajanların yazdığı    #
#  tezler ve online model ağırlıkları her deployda çöpe gider —         #
#  AI'lar boşa strateji geliştirmiş olur.                               #
#                                                                       #
#  ÇÖZÜM: DATABASE_URL varsa durum Postgres'e yazılır (deploy'dan       #
#  bağımsız, kalıcı). Yoksa dosyaya düşülür (yerel geliştirme için      #
#  doğru davranış). Hangi arka ucun kullanıldığı dashboard'da görünür.  #
# ==================================================================== #
def _normalize_db_url(url: str) -> str:
    """
    Bağlantı URL'ini psycopg'nin kabul ettiği biçime getirir.

    `postgres://` ve `postgresql://` şemalarını libpq zaten kabul eder, ama
    SQLAlchemy tarzı `postgresql+psycopg://` biçimi hata verir. Render
    panelinden kopyala-yapıştır sırasında bu biçimin gelmesi mümkün
    olduğu için sürücü ekini temizliyoruz — aksi halde sistem sessizce
    dosya yedeğine düşer ve kullanıcı bunu deploy'da veri kaybederek
    öğrenir.
    """
    u = (url or "").strip()
    if "+" in u.split("://", 1)[0]:
        scheme, rest = u.split("://", 1)
        u = scheme.split("+", 1)[0] + "://" + rest
    return u


DATABASE_URL = _normalize_db_url(os.getenv("DATABASE_URL", ""))

# Durum sağlığı — dashboard bunu gösterir, böylece "geçmişim korundu mu?"
# sorusu tahmine değil ölçüme dayanır.
HEALTH: dict = {
    "backend": None,          # "postgres" | "file"
    "durable": False,         # deploy'dan sağ çıkar mı?
    "loaded": False,          # açılışta durum geri yüklendi mi
    "loaded_from": None,
    "state_age_at_load_sec": None,
    "restored_trades": 0,
    "restored_portfolios": 0,
    "last_save_at": None,
    "last_save_ok": None,
    "save_count": 0,
    "last_error": None,
}

_pg_ready = False


def _pg_connect():
    """Postgres bağlantısı açar. psycopg yoksa/başarısızsa None."""
    try:
        import psycopg
    except ImportError:
        logger.warning(
            "DATABASE_URL verildi ama 'psycopg' kurulu değil — "
            "dosya yedeğine düşülüyor (deploy'da durum KAYBOLUR)."
        )
        return None
    try:
        return psycopg.connect(DATABASE_URL, connect_timeout=10)
    except Exception as e:
        logger.error(f"Postgres bağlantısı kurulamadı: {e}")
        return None


def _pg_init() -> bool:
    """Durum tablosunu hazırlar (tek satır: id=1)."""
    global _pg_ready
    if _pg_ready:
        return True
    conn = _pg_connect()
    if conn is None:
        return False
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_state (
                    id        INTEGER PRIMARY KEY,
                    payload   TEXT NOT NULL,
                    saved_at  DOUBLE PRECISION NOT NULL
                )
            """)
        _pg_ready = True
        return True
    except Exception as e:
        logger.error(f"Postgres tablo hazırlanamadı: {e}")
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _backend_name() -> str:
    return "postgres" if DATABASE_URL else "file"


def _write_blob(payload: str, path: str) -> bool:
    """Durumu kalıcı ortama yazar (Postgres varsa oraya, yoksa dosyaya)."""
    if DATABASE_URL and _pg_init():
        conn = _pg_connect()
        if conn is not None:
            try:
                with conn, conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO bot_state (id, payload, saved_at)
                        VALUES (1, %s, %s)
                        ON CONFLICT (id) DO UPDATE
                          SET payload = EXCLUDED.payload,
                              saved_at = EXCLUDED.saved_at
                        """,
                        (payload, time.time()),
                    )
                return True
            except Exception as e:
                logger.error(f"Postgres'e yazılamadı: {e}")
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        # Postgres başarısız olduysa dosyaya da yazmayı dene (veri kaybetmemek için)

    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        f.write(payload)
    os.replace(tmp, path)  # atomik
    return True


def _read_blob(path: str):
    """Kalıcı ortamdan durumu okur. Yoksa None."""
    if DATABASE_URL and _pg_init():
        conn = _pg_connect()
        if conn is not None:
            try:
                with conn, conn.cursor() as cur:
                    cur.execute("SELECT payload FROM bot_state WHERE id = 1")
                    row = cur.fetchone()
                if row and row[0]:
                    return row[0], "postgres"
            except Exception as e:
                logger.error(f"Postgres'ten okunamadı: {e}")
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

    if os.path.exists(path):
        with open(path) as f:
            return f.read(), "file"
    return None


# Trade üzerinde diske yazılan alanlar. Denetim alanları da dahildir —
# yeniden başlatma sonrası "bu işlem neden böyle açıldı?" sorusunun cevabı
# kaybolmamalı.
_TRADE_FIELDS = (
    "strategy_name", "timestamp", "side", "entry_price", "amount", "shares",
    "fee", "payout_if_win", "result", "pnl", "resolved_at", "market_window",
    "resolve_at", "raw_confidence", "calibrated_confidence",
    "window_offset", "top_ask", "mid_price", "slippage_bps", "levels_used",
    "requested_amount", "liquidity_capped", "ev", "reasoning", "ai_initiated",
    "btc_price_at_entry", "chainlink_at_entry", "cl_divergence_bps", "outcome",
    # Erken çıkış
    "exit_type", "exit_price", "exit_top_bid", "exit_proceeds", "exit_reason",
    "exit_at", "exit_slippage_bps", "exit_levels_used", "exit_blocked_reason",
)


def _trade_to_row(t) -> dict:
    """Trade dataclass -> JSON satırı (tüm alanlar)."""
    return {f: getattr(t, f, None) for f in _TRADE_FIELDS}


def _row_to_trade(row: dict):
    """JSON satırı -> Trade dataclass (eksik/eski alanlara toleranslı)."""
    from paper_trader import Trade
    import dataclasses

    valid = {f.name for f in dataclasses.fields(Trade)}
    kwargs = {k: v for k, v in row.items() if k in valid and v is not None}
    # Zorunlu alanların güvenli varsayılanları (eski state dosyaları için)
    kwargs.setdefault("strategy_name", "")
    kwargs.setdefault("timestamp", 0.0)
    kwargs.setdefault("side", "UP")
    kwargs.setdefault("entry_price", 0.5)
    kwargs.setdefault("amount", 0.0)
    kwargs.setdefault("shares", 0.0)
    kwargs.setdefault("fee", 0.0)
    kwargs.setdefault("payout_if_win", 0.0)
    return Trade(**kwargs)


def save_state(paper_trader, agents, path: str = STATE_FILE, online_model=None) -> bool:
    """
    Tüm sistem durumunu atomik olarak diske yazar.
    Hata durumunda False döner ama ASLA exception fırlatmaz (trading durmasın).
    """
    try:
        portfolios = {}
        for name, p in paper_trader.portfolios.items():
            portfolios[name] = {
                "balance": p.balance,
                "total_trades": p.total_trades,
                "wins": p.wins,
                "losses": p.losses,
                "total_pnl": p.total_pnl,
                "peak_balance": p.peak_balance,
                "max_drawdown": p.max_drawdown,
                "trades": [_trade_to_row(t) for t in p.trades[-MAX_SAVED_TRADES:]],
                "pending_trades": [_trade_to_row(t) for t in p.pending_trades],
                "balance_history": p.balance_history[-200:],
            }

        agents_state = {}
        for a in agents:
            agents_state[a.strategy.name] = {
                "bet_size": a.strategy.bet_size,
                "min_ev": a.strategy.min_ev,
                "is_active": a.strategy.is_active,
                "total_signals": a.strategy.total_signals,
                "adaptations": a.adaptations[-20:],
                "trades_at_last_adapt": a._trades_at_last_adapt,
                "total_ai_calls": a.total_ai_calls,
                "ai_overrides": a.ai_overrides,
                # Strateji-özel ayarlanabilir parametreler (AI değiştirebilir)
                "tunables": a.strategy.get_tunables() if hasattr(a.strategy, "get_tunables") else {},
                # Güven kalibratörü (öğrenilmiş a, b + ham örnekler)
                "calibrator": a.strategy.calibrator.dump() if hasattr(a.strategy, "calibrator") else {},
                # Ajanın KENDİ yazdığı tez (öz-değerlendirmeyle gelişir) — kaybolmamalı
                "thesis": getattr(a, "thesis", ""),
                "reflections": getattr(a, "reflections", [])[-10:],
                "ai_initiated": getattr(a, "ai_initiated", 0),
                "trades_at_last_reflect": getattr(a, "_trades_at_last_reflect", 0),
                # Her işlemden öğrenilen koşullu tablo — kaybolmamalı
                "price_bucket_stats": getattr(a.strategy, "price_bucket_stats", {}),
                "early_exits": getattr(a.strategy, "early_exits", 0),
            }

        state = {
            "version": 2,
            "saved_at": time.time(),
            "portfolios": portfolios,
            "agents": agents_state,
            "online_model": online_model.dump() if online_model else None,
        }

        _write_blob(json.dumps(state), path)

        HEALTH["backend"] = _backend_name()
        HEALTH["durable"] = bool(DATABASE_URL and _pg_ready)
        HEALTH["last_save_at"] = time.time()
        HEALTH["last_save_ok"] = True
        HEALTH["save_count"] += 1
        HEALTH["last_error"] = None
        return True
    except Exception as e:
        logger.error(f"Durum kaydedilemedi: {e}")
        HEALTH["last_save_ok"] = False
        HEALTH["last_error"] = str(e)[:200]
        return False


def load_state(paper_trader, agents, path: str = STATE_FILE, online_model=None) -> bool:
    """
    Diskteki durumu yükler. Dosya yoksa/bozuksa sessizce False döner
    (sistem temiz başlar).
    """
    HEALTH["backend"] = _backend_name()

    blob = None
    try:
        blob = _read_blob(path)
    except Exception as e:
        logger.error(f"Durum okunamadı: {e}")

    if not blob:
        HEALTH["durable"] = bool(DATABASE_URL and _pg_ready)
        if DATABASE_URL:
            logger.info("Kayıtlı durum yok (Postgres boş) — temiz başlanıyor.")
        else:
            logger.warning(
                "Kayıtlı durum yok — temiz başlanıyor. "
                "UYARI: DATABASE_URL ayarlı değil; bulutta her deploy'da "
                "bakiye/geçmiş/AI öğrenmesi SIFIRLANIR."
            )
        return False

    raw, source = blob
    HEALTH["durable"] = bool(DATABASE_URL and _pg_ready)

    try:
        state = json.loads(raw)

        # Portföyler
        for name, pd_ in (state.get("portfolios") or {}).items():
            p = paper_trader.portfolios.get(name)
            if p is None:
                continue
            p.balance = pd_.get("balance", p.balance)
            p.total_trades = pd_.get("total_trades", 0)
            p.wins = pd_.get("wins", 0)
            p.losses = pd_.get("losses", 0)
            p.total_pnl = pd_.get("total_pnl", 0.0)
            p.peak_balance = pd_.get("peak_balance", p.balance)
            p.max_drawdown = pd_.get("max_drawdown", 0.0)
            p.trades = [_row_to_trade(r) for r in (pd_.get("trades") or [])]
            p.pending_trades = [_row_to_trade(r) for r in (pd_.get("pending_trades") or [])]
            p.balance_history = [tuple(x) for x in (pd_.get("balance_history") or [])]
            # Global log'u da doldur ki dashboard boş görünmesin
            paper_trader.global_trade_log.extend(p.trades[-100:])

        # Ajanlar (öğrenilmiş parametreler)
        for a in agents:
            ad = (state.get("agents") or {}).get(a.strategy.name)
            if not ad:
                continue
            a.strategy.bet_size = ad.get("bet_size", a.strategy.bet_size)
            a.strategy.min_ev = ad.get("min_ev", a.strategy.min_ev)
            a.strategy.is_active = ad.get("is_active", True)
            a.strategy.total_signals = ad.get("total_signals", 0)
            a.adaptations = ad.get("adaptations") or []
            a._trades_at_last_adapt = ad.get("trades_at_last_adapt", 0)
            a.total_ai_calls = ad.get("total_ai_calls", 0)
            a.ai_overrides = ad.get("ai_overrides", 0)
            tun = ad.get("tunables") or {}
            if tun and hasattr(a.strategy, "set_tunables"):
                a.strategy.set_tunables(tun)
            cal = ad.get("calibrator") or {}
            if cal and hasattr(a.strategy, "calibrator"):
                a.strategy.calibrator.load(cal)
            # Ajanın öğrendiği tez ve öz-değerlendirmeleri geri yükle
            a.thesis = ad.get("thesis", "")
            a.reflections = ad.get("reflections") or []
            a.ai_initiated = ad.get("ai_initiated", 0)
            a._trades_at_last_reflect = ad.get("trades_at_last_reflect", 0)
            a.strategy.price_bucket_stats = ad.get("price_bucket_stats") or {}
            a.strategy.early_exits = ad.get("early_exits", 0)

        # Online model (öğrenilmiş ağırlıklar)
        if online_model is not None and state.get("online_model"):
            online_model.load(state["online_model"])
            logger.info(f"📚 Online model geri yüklendi: {online_model.n_updates} pencere")

        age = time.time() - state.get("saved_at", 0)
        n_trades = sum(len(p.trades) + len(p.pending_trades)
                       for p in paper_trader.portfolios.values())
        HEALTH.update({
            "loaded": True,
            "loaded_from": source,
            "state_age_at_load_sec": round(age, 1),
            "restored_trades": n_trades,
            "restored_portfolios": len(state.get("portfolios") or {}),
        })
        logger.info(
            f"✅ Kayıtlı durum yüklendi [{source}] "
            f"({age/60:.0f} dk önce kaydedilmiş) — "
            f"{n_trades} işlem, {HEALTH['restored_portfolios']} portföy geri geldi"
        )
        return True

    except Exception as e:
        logger.error(f"Durum yüklenemedi ({e}) — temiz başlanıyor.")
        HEALTH["last_error"] = str(e)[:200]
        return False
