# Polymarket BTC 5m — Multi-Strategy AI Trading System

Polymarket'in "Bitcoin Up or Down (5 dakika)" piyasalarında **kağıt üzerinde** (paper
trading) işlem yapan, 6 stratejinin birbiriyle yarıştığı ve her stratejinin başına bir
AI ajanı (Gemini) koyulmuş canlı bir araştırma/izleme sistemi.

> ⚠️ **Gerçek para kullanmaz.** Sistem sanal $1000 bakiye ile simülasyon yapar.

---

## Hızlı başlangıç

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # GEMINI_API_KEY'ini gir (opsiyonel — yoksa AI kapalı çalışır)
python main.py
```

Ardından: **http://localhost:5050**

---

## Mimari

```
main.py                 Orkestratör: veri → strateji → AI → paper trade → resolution
market_data.py          Tüm piyasa verisi (Binance + Polymarket)
strategies/             6 bağımsız strateji
ai_agent.py             Her stratejinin AI ajanı (inceleme + adaptasyon)
paper_trader.py         Sanal portföy, P&L, kazanma oranı, drawdown
persistence.py          Restart'ta durum kaybolmasın (state.json)
dashboard/              Canlı web arayüzü (Flask + vanilla JS)
```

### Veri kaynakları (hepsi gerçek, canlı)

| Kaynak | Ne sağlar | Neden önemli |
|---|---|---|
| Binance `@trade` WS | Fiyat, 1dk mumlar, **CVD** | Agresif alış/satış akışı — bilgi burada görünür |
| Binance `@depth20` WS | Emir defteri (20 seviye) | Pasif likidite: kalın bid = destek |
| Binance futures REST | **Funding rate**, mark, basis | Kalabalık konumlanma (kontrarian sinyal) |
| Binance `openInterest` | Açık pozisyon | Kaldıraç birikimi / squeeze tespiti |
| Binance `@forceOrder` WS | Likidasyonlar | Zorunlu akış = kısa vadeli itici güç |
| Polymarket Gamma/CLOB | Piyasa, midpoint, order book | İşlem yapılacak fiyat + implied olasılık |

### Stratejiler

1. **Momentum RSI+EMA** — trend takibi
2. **VWAP Mean Reversion** — VWAP'tan sapma sonrası dönüş
3. **Order Book Imbalance** — Binance derinlik dengesizliği + CVD teyidi
4. **Bollinger Breakout** — volatilite sıkışması/patlaması
5. **Implied Prob. Arbitrage** — kendi modeli vs Polymarket fiyatı
6. **Microstructure Alpha** — CVD + defter + funding + OI'yi birleştiren çok kanallı model

---

## Risk kontrolleri

- **Fiyat bandı**: $0.10–$0.90 dışında işlem yok (sonuç neredeyse belliyken alım yapılmaz)
- **Pencere sonu**: son 60 saniyede yeni pozisyon açılmaz
- **Pencere başına tek pozisyon**: aynı 5dk'da yığılma yok
- **Fraksiyonel Kelly**: bahis, edge büyüklüğüyle orantılı (bakiyenin max %20'si)
- **Gerçekçi maliyet**: midpoint değil, gerçekten ödenecek **ask** fiyatı kullanılır

## Öğrenme katmanları

Sistemde üç ayrı öğrenme mekanizması var:

### 1. Güven Kalibrasyonu (`calibration.py`)
**Çözdüğü sorun:** Stratejiler güveni uydurma formüllerle üretir
(`0.52 + strength*0.15`). Bu sayının gerçekle ilgisi olmayabilir — strateji
"%60 eminim" derken gerçekte %48 tutturuyorsa **tüm EV hesabı yanlıştır** ve
sistem yapısal olarak kaybeder.

**Çözüm:** Her (tahmin, gerçek sonuç) çifti kaydedilir; Platt scaling ile
ham güven ampirik gerçeğe hizalanır. Dashboard'da her strateji için
"Dediği: %58 / Gerçek: %51 → fazla iyimser" şeklinde görünür.
Brier skoru ile ölçülür (0.25 = rastgele, düşük = iyi).

### 2. Online Öğrenen Model (`online_model.py`)
Diğer stratejilerin aksine **hiçbir kural yazılmamıştır**. Her 5dk penceresi
kapandığında şunu öğrenir:

```
girdi  = pencere BAŞINDAKİ mikroyapı (CVD, defter, funding, OI, momentum…)
hedef  = BTC gerçekte yukarı mı kapandı?
```

Artımlı lojistik regresyon (SGD). Zamanla hangi sinyalin gerçekten öngörü
taşıdığını kendi keşfeder; öğrendiği ağırlıklar dashboard'da görünür.

- **Polymarket'ten bağımsız çalışır** — piyasa erişilemezken bile öğrenir
- 25 pencere görmeden tahmin vermez
- `log_loss` rastgeleden (0.693) iyi değilse **işlem açmaz** (sahte güven yok)
- Öğrenme hızı: ~12 pencere/saat → anlamlı veri için sistemin sürekli çalışması gerekir

### 3. AI ajanları

Her stratejinin başında bir Gemini ajanı var:
- **İnceleme**: işlem öncesi kararı gözden geçirir, gerekirse bloklar
- **Hızlı adaptasyon** (kural): kazanınca agresif, kaybedince seçici (bet/minEV ayarı)
- **Derin ayar** (Gemini, ~10dk'da bir): işlem geçmişi + mikroyapıyı okuyup
  **stratejiye özel eşikleri** (kanal ağırlıkları, dengesizlik eşiği…) gerekçeyle değiştirir

Değişiklikler her parametrenin kendi `min/max` sınırı içinde kalır — güvenli.

---

## Resolution (sonuç belirleme)

1. **Öncelik**: Piyasanın kendi kararı — pencere kapanırken UP fiyatı ≥0.85 ise UP kazandı
2. **Yedek**: Pencere açılış/kapanış BTC fiyatı kıyası (1dk mum sınırından hassas)

---

## Deploy (7/24 çalışma)

`render.yaml` hazır. Render → **New +** → **Blueprint** → repoyu bağla.

> ⚠️ **`region: frankfurt` şart** — Binance ABD IP'lerini engeller.
> ⚠️ Free tier 15 dk inaktivitede uyur. Sürekli çalışması için Starter plan
> veya `/healthz` adresine harici keep-alive ping (ör. cron-job.org, 10 dk).

`GEMINI_API_KEY` Render panelinden **gizli** env olarak girilmeli — repoya yazma.

---

## 🔍 Dürüstlük notu (önemli)

Bu sistem **kanıtlanmış bir kâr avantajı (edge) içermiyor.**

Önceki kapsamlı araştırmada (LSTM + teknik indikatörler + funding + order-flow,
temiz walk-forward out-of-sample testlerle) 1s/4s/1g zaman dilimlerinde BTC yön
tahmininde **sömürülebilir edge bulunamadı** — test doğruluğu sürekli rastgele
seviyesinde (%33–37) kaldı. Bu bir kod kusuru değil; kısa vadeli BTC yönü rastgele
yürüyüşe çok yakındır ve bedava veri herkese açıktır.

**Bu sistemin gerçek değeri:**
- Gerçek piyasa mikroyapısını canlı izlemek ve öğrenmek
- Stratejileri sızıntısız, gerçekçi maliyetlerle karşılaştırmak
- AI ajanlarının adaptasyonunu gözlemlemek

**Beklenti yönetimi:** Kısa vadeli `+%X` sonuçlar istatistiksel gürültüdür. Bir
stratejiye güvenmeden önce **yüzlerce işlem** üzerinde tutarlı pozitif P&L arayın.
Gerçek para bağlamadan önce uzun süreli paper trading şart.
