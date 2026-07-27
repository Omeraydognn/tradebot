import os
from dotenv import load_dotenv

load_dotenv()

# Polymarket
POLYMARKET_PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY")
POLYMARKET_ADDRESS = os.getenv("POLYMARKET_ADDRESS")

# Binance
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")

# AI (Gemini)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
# Ücretsiz katman MODEL BAŞINA günde ~20 istek verir. Tek model kullanınca
# kota dakikalar içinde biter. Birden fazla modeli sırayla kullanmak kotayı
# aşmak değildir — her modelin kendi ayrı kotası vardır ve bu meşrudur.
# Biri dolunca (429) otomatik olarak sıradakine geçilir.
GEMINI_MODELS = [
    m.strip() for m in os.getenv(
        "GEMINI_MODELS",
        "gemini-flash-lite-latest,gemini-3.5-flash-lite,gemini-3.1-flash-lite,"
        "gemini-3-flash-preview,gemini-flash-latest",
    ).split(",") if m.strip()
]
# Geriye dönük uyumluluk (tek model bekleyen kodlar için)
GEMINI_MODEL = os.getenv("GEMINI_MODEL", GEMINI_MODELS[0])
# Kota dostu: her ajan en fazla bu aralıkta bir AI çağrısı yapar (saniye)
AI_MIN_INTERVAL_SEC = float(os.getenv("AI_MIN_INTERVAL_SEC", "20"))

# Paper Trading
INITIAL_BALANCE = float(os.getenv("INITIAL_BALANCE", "1000.0"))  # $1000 starting balance per strategy
# Polymarket CLOB'da açık bir "işlem ücreti" YOKTUR; gerçek maliyet spread'dir
# (midpoint yerine ask fiyatından alım — bu artık modelleniyor).
# Yine de küçük bir sürtünme payı bırakmak istersen bunu >0 yap.
POLYMARKET_FEE_RATE = float(os.getenv("POLYMARKET_FEE_RATE", "0.0"))

# --- İşlem koruma bantları (guardrails) ---
# Fiyat bu bandın DIŞINDAysa işlem açma (Implied Arb'ın $0.01'e DOWN alıp
# batmasını önler — uçtaki fiyat = sonuç neredeyse belli).
# Yalnızca piyasanın GERÇEKTEN kararsız olduğu bantta işlem yap.
# $0.18'de alım = piyasa "%18 ihtimal" diyor; orada ona karşı gelmek için
# kanıtlanmış bir avantaj gerekir — bizde yok. Üretimde bu tür işlemlerin
# sistematik zarar ettiği gözlendi (piyasa 0.07'ye giderken UP alınıyordu).
PRICE_MIN = float(os.getenv("PRICE_MIN", "0.25"))
PRICE_MAX = float(os.getenv("PRICE_MAX", "0.75"))
# Pencere sonuna bu kadar saniyeden az kaldıysa yeni işlem açma.
# 5 dakikalık pencerenin son 2 dakikasında sonuç büyük ölçüde belirlenmiştir;
# 60sn çok geçti (65sn kala açılan işlemler piyango bileti gibiydi).
NO_TRADE_LAST_SECONDS = float(os.getenv("NO_TRADE_LAST_SECONDS", "120"))

# --- Adaptif AI ---
# Her strateji bu kadar SONUÇLANMIŞ işlemde bir kendini yeniden ayarlar.
ADAPT_EVERY_N_TRADES = int(os.getenv("ADAPT_EVERY_N_TRADES", "3"))

# --- Zorunlu işlem modu ---
# 25 Temmuz'daki "piyasaya karşı gitme" düzeltmesi (bkz. 96dd116) EV eşiğini
# ve persona veto'sunu o kadar sıkılaştırdı ki sistem GÜN BOYU tek işlem
# açmadı (state.json: 7 stratejiden 5'i total_trades=0). Öğrenme döngüsü
# (kalibratör, adapt(), reflect()) sonuçlanmış işlem olmadan asla beslenmez.
# FORCE_TRADE_MODE açıkken: fiyat/zaman/pencere koruma bantları AYNEN
# kalır (uçtaki fiyata veya pencere sonuna hâlâ girilmez — bunlar gerçek
# bilgi taşımaz), ama EV eşiği ve persona veto'su işlemi ENGELLEMEZ; sadece
# bahis boyutunu/güveni etkiler. Amaç: her strateji her pencerede bir
# pozisyon alsın, kazansın da kaybetsin de — kalibratör ve adaptasyon
# gerçek veriyle beslensin.
FORCE_TRADE_MODE = os.getenv("FORCE_TRADE_MODE", "false").lower() in ("1", "true", "yes")

# Dashboard — Render/bulut $PORT verirse onu kullan, yoksa 5050
DASHBOARD_PORT = int(os.getenv("PORT") or os.getenv("DASHBOARD_PORT", "5050"))
