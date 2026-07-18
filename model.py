"""
model.py — Yapay Zeka Çekirdeği

Finansal zaman serilerindeki (OHLCV gibi) ardışık fiyat kalıplarını
öğrenmek için temel bir PyTorch LSTM modeli.
"""

import torch
import torch.nn as nn


class PriceLSTM(nn.Module):
    """
    Zaman serisi tahmini için LSTM tabanlı sinir ağı.

    Parametreler
    ----------
    input_size : int
        Her zaman adımındaki özellik sayısı (örn. OHLCV için 5).
    hidden_size : int
        LSTM gizli katmanındaki nöron (birim) sayısı.
    num_layers : int
        Üst üste dizilmiş (stacked) LSTM katman sayısı.
    output_size : int
        Çıkış boyutu (örn. tek bir sonraki fiyat için 1).
    dropout : float, optional
        Katmanlar arası dropout oranı (num_layers > 1 iken etkilidir).
    """

    def __init__(self, input_size, hidden_size, num_layers, output_size, dropout=0.2):
        super(PriceLSTM, self).__init__()

        self.input_size = input_size
        self.hidden_size = hidden_size
        self.num_layers = num_layers
        self.output_size = output_size

        # batch_first=True  ->  giriş/çıkış şekli: (batch, seq_len, features)
        self.lstm = nn.LSTM(
            input_size=input_size,
            hidden_size=hidden_size,
            num_layers=num_layers,
            batch_first=True,
            dropout=dropout if num_layers > 1 else 0.0,
        )

        # LSTM'in son zaman adımı çıktısını hedef boyuta indirger
        self.fc = nn.Linear(hidden_size, output_size)

    def forward(self, x):
        """
        İleri geçiş (forward pass).

        x : Tensor, şekli (batch, seq_len, input_size)
        döner : Tensor, şekli (batch, output_size)
        """
        batch_size = x.size(0)

        # Başlangıç gizli durumu (h0) ve hücre durumu (c0) — sıfırla başlat
        h0 = torch.zeros(self.num_layers, batch_size, self.hidden_size, device=x.device)
        c0 = torch.zeros(self.num_layers, batch_size, self.hidden_size, device=x.device)

        # LSTM'den geçir
        # out şekli: (batch, seq_len, hidden_size)
        out, (hn, cn) = self.lstm(x, (h0, c0))

        # Yalnızca son zaman adımının çıktısını al ve tam bağlı katmana ver
        last_step = out[:, -1, :]        # (batch, hidden_size)
        prediction = self.fc(last_step)  # (batch, output_size)

        return prediction


# Takma ad: projedeki tutarlılık için PriceLSTM = TradeAILSTM
TradeAILSTM = PriceLSTM


if __name__ == "__main__":
    # --- Basit boyut testi ---
    # Örnek senaryo: 5 özellikli (OHLCV), 30 adımlık geçmiş pencere,
    # tek bir sonraki değer tahmini.
    input_size = 5     # O, H, L, C, V
    hidden_size = 64
    num_layers = 2
    output_size = 1
    seq_len = 30
    batch_size = 16

    model = PriceLSTM(input_size, hidden_size, num_layers, output_size)
    print(model)

    # Rastgele örnek girdi: (batch, seq_len, input_size)
    dummy_input = torch.randn(batch_size, seq_len, input_size)
    output = model(dummy_input)

    print("\n--- Boyut Testi ---")
    print(f"Girdi şekli : {tuple(dummy_input.shape)}")
    print(f"Çıktı şekli : {tuple(output.shape)}")
    print(f"Beklenen    : ({batch_size}, {output_size})")

    assert output.shape == (batch_size, output_size), "Boyut uyuşmazlığı!"
    print("Test BAŞARILI ✅")
