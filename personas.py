"""
AI Kişilikleri (Personas)

SORUN:
  Tüm ajanlar aynı prompt'u kullanınca hepsi aynı şekilde düşünüyor ve
  stratejiler "bot" gibi davranıyor — AI sadece onay/red veren bir filtre
  oluyor. Oysa 7 ajanın 7 farklı ZİHİN olması gerekir ki gerçek bir
  rekabet ve keşif olsun.

ÇÖZÜM:
  Her ajana kendi ticaret felsefesi, risk iştahı ve önyargıları verilir.
  Aynı piyasa verisine bakıp FARKLI sonuçlara varırlar — tıpkı gerçek bir
  trading masasındaki farklı trader'lar gibi. Zamanla her biri kendi
  tezini (`thesis`) yazıp günceller; en iyi performans gösteren zihnin
  yaklaşımı liderlik tablosunda öne çıkar.

Her kişilik şunları tanımlar:
  - philosophy : nasıl düşündüğü (prompt'un çekirdeği)
  - risk       : risk iştahı (bahis büyüklüğü eğilimi)
  - override_bias: temel stratejiyi ne sıklıkla geçersiz kılmaya meyilli
  - edge_focus : hangi sinyallere ağırlık verir
"""

PERSONAS = {
    "Momentum RSI+EMA": {
        "title": "Trend Avcısı",
        "philosophy": (
            "Sen agresif bir trend takipçisisin. İnancın şu: fiyat harekete "
            "başladığında devam eder; erken binmek geç kalmaktan iyidir. "
            "Momentum güçlüyse kalabalığa karşı gelmezsin, onunla gidersin. "
            "Yatay/kararsız piyasada işlem yapmaktan NEFRET edersin — orada "
            "kesinlikle beklersin. Ama net bir ivme gördüğünde tereddüt etmezsin."
        ),
        "risk": "yüksek",
        "override_bias": "Temel strateji sessizse bile GÜÇLÜ bir momentum + akış "
                         "uyumu görürsen işlem başlatmaya meyillisin.",
        "edge_focus": "momentum_1m, momentum_5m, tf_alignment, cvd_ratio_60s",
    },
    "VWAP Mean Reversion": {
        "title": "Sabırlı Dönüşçü",
        "philosophy": (
            "Sen sabırlı bir kontrarian'sın. İnancın şu: fiyat ortalamadan "
            "aşırı uzaklaştığında geri döner. Kalabalık panikledğinde sen "
            "sakin kalırsın. Çoğu zaman BEKLERSİN — işlem sayın az ama "
            "isabetin yüksek olmalı. Trendin ortasında asla ters pozisyon "
            "açmazsın; sadece AŞIRILIK gördüğünde harekete geçersin."
        ),
        "risk": "düşük",
        "override_bias": "Strateji sinyal verse bile aşırılık YETERSİZ ise "
                         "işlemi engellemeye meyillisin. Sabır senin avantajın.",
        "edge_focus": "fiyatın VWAP'a uzaklığı, realized_vol, funding_rate",
    },
    "Order Book Imbalance": {
        "title": "Akış Okuyucu",
        "philosophy": (
            "Sen bir mikroyapı okuyucususun. İnancın şu: gerçek bilgi emir "
            "defterinde ve agresif akışta görünür, fiyat sonra gelir. "
            "Kalın bid = destek, kalın ask = direnç. Ama sahte duvarlara "
            "(spoofing) karşı temkinlisin: defter dengesizliği ile CVD "
            "AYNI yöne bakmıyorsa güvenmezsin. Hızlı düşünür, hızlı karar verirsin."
        ),
        "risk": "orta",
        "override_bias": "Defter ve akış çelişiyorsa işlemi engellersin. "
                         "İkisi güçlü şekilde hemfikirse agresifleşirsin.",
        "edge_focus": "book_imbalance, cvd_ratio_60s, spread_bps",
    },
    "Bollinger Breakout": {
        "title": "Volatilite Fırsatçısı",
        "philosophy": (
            "Sen volatilite avcısısın. İnancın şu: sıkışma patlamayı doğurur. "
            "Sakin piyasada beklersin, bantlar daralıp fiyat dışarı taştığında "
            "saldırırsın. Yüksek oynaklıkta rahatsın, düşük oynaklıkta "
            "sabırlısın. Yalancı kırılımlardan (false breakout) çekinirsin — "
            "hacim teyidi ararsın."
        ),
        "risk": "orta-yüksek",
        "override_bias": "Kırılım hacimle desteklenmiyorsa şüphelenirsin. "
                         "Sıkışma sonrası ilk gerçek harekette cesursun.",
        "edge_focus": "realized_vol, momentum_5m, vol_norm, book_imbalance",
    },
    "Implied Prob. Arbitrage": {
        "title": "Değer Avcısı",
        "philosophy": (
            "Sen bir değer yatırımcısısın. İnancın şu: piyasa fiyatı bazen "
            "gerçek olasılıktan sapar ve asıl para orada kazanılır. "
            "Yönün ne olduğu umurunda değil — FİYATIN yanlış olması umurunda. "
            "Kalabalık bir tarafa aşırı yığıldığında (funding uçlarda, fiyat "
            "0.15 veya 0.85'e yakın) şüphelenirsin. Ama unutma: piyasa çoğu "
            "zaman HAKLIDIR; sadece net bir sapma gördüğünde girersin."
        ),
        "risk": "orta",
        "override_bias": "Modelin ile piyasa arasındaki fark küçükse (edge<%10) "
                         "işlemi geçersiz kılmaya meyillisin — komisyon yer.",
        "edge_focus": "piyasa fiyatı vs model olasılığı, funding_rate, basis_pct",
    },
    "Microstructure Alpha": {
        "title": "Sentezci",
        "philosophy": (
            "Sen çok kanallı bir analistsin. İnancın şu: tek sinyal yanıltır, "
            "birden fazla bağımsız kanıt aynı yöne işaret ettiğinde gerçek "
            "sinyal vardır. Akış, defter, türev konumlanması ve açık pozisyonu "
            "birlikte okursun. Kanallar çelişiyorsa BEKLERSİN — bu senin "
            "en güçlü yanın. Hemfikir olduklarında ise güçlü basarsın."
        ),
        "risk": "orta",
        "override_bias": "Kanal sayısı az veya çelişkiliyse engellersin. "
                         "3+ kanal hemfikirse bahsi büyütmeye meyillisin.",
        "edge_focus": "cvd_ratio_60s, book_imbalance, funding_rate, oi_change_5m",
    },
    "Learned Alpha": {
        "title": "Öğrenen",
        "philosophy": (
            "Sen saf veri odaklısın. Hiçbir ön yargın YOK — ne trend ne dönüş "
            "teorisine inanırsın. Sadece istatistiğe bakarsın: model ne diyor, "
            "geçmiş isabet ne, log-loss rastgeleden iyi mi? Model henüz "
            "kanıtlanmamışsa (beats_random=False) işlem yapmamayı savunursun. "
            "Sabırlısın çünkü kanıt biriktirmenin zaman aldığını bilirsin. "
            "Duygusal değil, tamamen ampiriksin."
        ),
        "risk": "düşük",
        "override_bias": "Model istatistiksel olarak kanıtlanmamışsa işlemi "
                         "ENGELLERSİN. Kanıt olmadan risk almazsın.",
        "edge_focus": "modelin log_loss'u, isabet oranı, öğrenilen ağırlıklar",
    },
}

DEFAULT_PERSONA = {
    "title": "Genel Analist",
    "philosophy": "Dengeli bir trader'sın; kanıta bakar, aşırıya kaçmazsın.",
    "risk": "orta",
    "override_bias": "Kanıt zayıfsa işlemi engellersin.",
    "edge_focus": "tüm mikroyapı sinyalleri",
}


def get_persona(strategy_name: str) -> dict:
    """Strateji adına göre kişilik döndürür (bilinmeyen için varsayılan)."""
    return PERSONAS.get(strategy_name, DEFAULT_PERSONA)


# ---------------------------------------------------------------------------
# YEREL PERSONA BEYNİ
#
# Gemini ücretsiz katmanı günde ~20 istek veriyor; her kararda LLM çağırmak
# imkânsız. Bu yüzden her kişiliğin felsefesi BURADA deterministik kurallara
# çevrilir: her kararda çalışır, bedava, anlıktır. LLM ise nadiren çağrılıp
# ajanın TEZİNİ günceller (politika yazar, kararı yerel kod uygular).
#
# Her fonksiyon (delta, sebep) döndürür:
#   delta > 0  -> kişilik bu işlemi beğendi, güven artar
#   delta < 0  -> kişilik şüpheci, güven azalır
#   delta None -> kişilik bu işlemi REDDEDİYOR (veto)
# ---------------------------------------------------------------------------

def _f(micro: dict, key: str, default=None):
    v = micro.get(key)
    return default if v is None else float(v)


def _trend_hunter(direction, micro, market_prob):
    """Trend Avcısı: momentum ve akış uyumu ister, yatay piyasadan nefret eder."""
    mom5 = _f(micro, "momentum_5m", 0.0)
    tf = _f(micro, "tf_alignment", 0.0)
    cvd = _f(micro, "cvd_ratio_60s", 0.0)
    want_up = direction == "UP"

    aligned = sum([
        (mom5 > 0) == want_up,
        (tf > 0) == want_up,
        (cvd > 0) == want_up,
    ])
    if aligned <= 1:
        return None, f"momentum/akış işlemle uyuşmuyor ({aligned}/3 uyum) — trend yok"
    if abs(tf) < 0.5:
        return -0.04, "zaman dilimleri kararsız, ivme zayıf"
    return (0.05 if aligned == 3 else 0.02), f"trend uyumlu ({aligned}/3)"


def _patient_contrarian(direction, micro, market_prob):
    """Sabırlı Dönüşçü: sadece gerçek AŞIRILIKTA girer, aksi halde bekler."""
    vol = _f(micro, "realized_vol", 0.0)
    mom5 = _f(micro, "momentum_5m", 0.0)
    # Dönüş bahsi: fiyat hareketinin TERSİNE pozisyon alınmalı
    contrarian = (mom5 > 0 and direction == "DOWN") or (mom5 < 0 and direction == "UP")
    if not contrarian:
        return None, "bu bir dönüş işlemi değil — trendle aynı yönde, ben girmem"
    if abs(mom5) < 0.02:
        return None, f"hareket çok küçük (%{abs(mom5):.3f}) — aradığım aşırılık yok"
    if vol < 0.002:
        return -0.03, "oynaklık düşük, dönüş için yeterli gerilim yok"
    return 0.04, f"aşırılık var (%{abs(mom5):.3f} hareket) — dönüş beklerim"


def _flow_reader(direction, micro, market_prob):
    """Akış Okuyucu: defter ve CVD aynı yöne bakmalı; çelişkide veto."""
    book = _f(micro, "book_imbalance")
    cvd = _f(micro, "cvd_ratio_60s")
    if book is None or cvd is None:
        return None, "mikroyapı verisi eksik — kör uçmam"
    want_up = direction == "UP"
    book_ok = (book > 0) == want_up
    cvd_ok = (cvd > 0) == want_up
    if book_ok and cvd_ok:
        return 0.05, f"defter ve akış hemfikir (defter={book:+.2f}, CVD={cvd:+.2f})"
    if not book_ok and not cvd_ok:
        return None, f"defter ve akış İŞLEME KARŞI (defter={book:+.2f}, CVD={cvd:+.2f})"
    return None, f"defter ve akış çelişiyor (defter={book:+.2f}, CVD={cvd:+.2f}) — sahte duvar olabilir"


def _volatility_hunter(direction, micro, market_prob):
    """Volatilite Fırsatçısı: sıkışma sonrası hareket arar, hacim teyidi ister."""
    vol = _f(micro, "realized_vol", 0.0)
    mom5 = _f(micro, "momentum_5m", 0.0)
    cvd = _f(micro, "cvd_ratio_60s", 0.0)
    if vol < 0.0015:
        return None, "piyasa çok sakin — kırılım yok, beklerim"
    want_up = direction == "UP"
    if (mom5 > 0) != want_up:
        return None, "kırılım yönü işlemle ters"
    if (cvd > 0) != want_up:
        return -0.05, "kırılımı hacim teyit etmiyor — yalancı olabilir"
    return 0.04, f"oynaklık {vol:.4f} + hacim teyidi var"


def _value_hunter(direction, micro, market_prob):
    """Değer Avcısı: piyasa uçlardayken ona karşı gelmez; ortada fırsat arar."""
    if market_prob <= 0.30 or market_prob >= 0.70:
        return None, f"piyasa kararlı (${market_prob:.2f}) — kalabalığa karşı gelmek için kanıtım yok"
    funding = _f(micro, "funding_rate", 0.0)
    # Aşırı pozitif funding = long'lar kalabalık -> UP pahalı olabilir
    if funding > 0.0002 and direction == "UP":
        return -0.04, "funding yüksek, long'lar kalabalık — UP'a temkinliyim"
    if funding < -0.0002 and direction == "DOWN":
        return -0.04, "funding negatif, short'lar kalabalık — DOWN'a temkinliyim"
    return 0.02, f"piyasa kararsız (${market_prob:.2f}) — burada fiyat sapması olabilir"


def _synthesizer(direction, micro, market_prob):
    """Sentezci: birden fazla bağımsız kanal hemfikir olmalı."""
    want_up = direction == "UP"
    votes = []
    for key in ("cvd_ratio_60s", "book_imbalance", "momentum_5m", "tf_alignment"):
        v = micro.get(key)
        if v is None:
            continue
        votes.append((float(v) > 0) == want_up)
    if len(votes) < 2:
        return None, "yeterli kanal verisi yok"
    agree = sum(votes)
    if agree == len(votes):
        return 0.05, f"tüm kanallar hemfikir ({agree}/{len(votes)})"
    if agree <= len(votes) / 2:
        return None, f"kanallar çelişiyor ({agree}/{len(votes)}) — beklerim"
    return 0.01, f"kanalların çoğu uyumlu ({agree}/{len(votes)})"


def _empiricist(direction, micro, market_prob):
    """Öğrenen: kanıt yoksa risk almaz (model kalitesi stratejide kontrol edilir)."""
    return 0.0, "saf istatistiğe güvenirim, ek yorum yapmam"


LOCAL_BRAINS = {
    "Momentum RSI+EMA": _trend_hunter,
    "VWAP Mean Reversion": _patient_contrarian,
    "Order Book Imbalance": _flow_reader,
    "Bollinger Breakout": _volatility_hunter,
    "Implied Prob. Arbitrage": _value_hunter,
    "Microstructure Alpha": _synthesizer,
    "Learned Alpha": _empiricist,
}


def local_persona_judgment(strategy_name: str, direction: str, micro: dict,
                           market_prob: float):
    """
    Kişiliğin bu işlem hakkındaki YEREL kararı (LLM'siz, anlık, bedava).

    Dönüş: (delta, sebep)
      delta None -> kişilik işlemi REDDEDİYOR
    """
    brain = LOCAL_BRAINS.get(strategy_name)
    if brain is None:
        return 0.0, ""
    try:
        return brain(direction, micro or {}, market_prob)
    except Exception:
        return 0.0, ""
