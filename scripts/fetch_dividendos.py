"""
Busca dividendos:
  - Ações: PlayInvest (HTML scraping)
  - FIIs: Yahoo Finance via yfinance (API JSON)
Grava no Firestore. Roda como GitHub Action 1x por dia.
"""
import os
import json
import time
import requests
from bs4 import BeautifulSoup
from datetime import datetime
import firebase_admin
from firebase_admin import credentials, firestore
import yfinance as yf

DELAY = 2
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

# ============ FIREBASE ============
def init_firebase():
    cred = credentials.Certificate(json.loads(os.environ["FIREBASE_CREDENTIALS"]))
    firebase_admin.initialize_app(cred)
    return firestore.client()

# ============ PLAYINVEST (AÇÕES) ============
def fetch_playinvest(ticker):
    url = f"https://playinvest.com.br/dividendos/{ticker.lower()}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            return []
        soup = BeautifulSoup(resp.text, "html.parser")
        divs = []
        for row in soup.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) < 4:
                continue
            dc = parse_date_br(cells[0].get_text(strip=True))
            dp = parse_date_br(cells[2].get_text(strip=True))
            tipo = cells[3].get_text(strip=True)
            val_text = cells[4].get_text(strip=True) if len(cells) > 4 else cells[1].get_text(strip=True)
            try:
                rate = float(val_text.replace(",", "."))
            except:
                continue
            if dc and rate > 0:
                divs.append({"dc": dc, "dp": dp or "", "r": rate, "l": tipo})
        return divs
    except Exception as e:
        print(f"    PlayInvest erro: {e}")
        return []

# ============ INVESTIDOR10 (FIIs + ETFs — tem data com E data pagamento) ============
def fetch_investidor10(ticker, tipo="fiis"):
    """Busca dividendos do investidor10.com.br — retorna data com E data pagamento."""
    url = f"https://investidor10.com.br/{tipo}/{ticker.lower()}/"
    try:
        resp = requests.get(url, headers={**HEADERS, "Referer": "https://investidor10.com.br/"}, timeout=15)
        if resp.status_code != 200:
            return []
        
        soup = BeautifulSoup(resp.text, "html.parser")
        divs = []
        
        # Procurar tabela de proventos/rendimentos
        tables = soup.find_all("table")
        for table in tables:
            headers_text = [th.get_text(strip=True).upper() for th in table.find_all("th")]
            # Identificar tabela com colunas de dividendos
            has_data_com = any("COM" in h or "BASE" in h for h in headers_text)
            has_valor = any("VALOR" in h or "R$" in h for h in headers_text)
            
            if not (has_data_com and has_valor):
                continue
            
            for row in table.find_all("tr"):
                cells = row.find_all("td")
                if len(cells) < 3:
                    continue
                
                # Tentar extrair: tipo, data com, data pgto, valor
                texts = [c.get_text(strip=True) for c in cells]
                
                dc = None
                dp = None
                rate = 0
                label = "RENDIMENTO"
                
                for txt in texts:
                    # Detectar datas (DD/MM/YYYY)
                    if "/" in txt and len(txt) >= 8:
                        parsed = parse_date_br(txt)
                        if parsed:
                            if not dc:
                                dc = parsed
                            elif not dp:
                                dp = parsed
                    # Detectar valor monetário
                    elif txt.replace(",", "").replace(".", "").replace("R$", "").replace(" ", "").replace("-","").isdigit() or \
                         any(c.isdigit() for c in txt):
                        try:
                            val = float(txt.replace("R$", "").replace(" ", "").replace(".", "").replace(",", "."))
                            if 0 < val < 100:  # valor por cota razoável
                                rate = val
                        except:
                            pass
                    # Detectar tipo
                    elif txt.upper() in ("DIVIDENDO", "JCP", "RENDIMENTO", "JSCP"):
                        label = txt.upper()
                
                if dc and rate > 0:
                    divs.append({
                        "dc": dc,
                        "dp": dp or estimate_payment_date(dc, 10),
                        "r": round(rate, 6),
                        "l": label
                    })
        
        return divs
    except Exception as e:
        print(f"    Investidor10 erro: {e}")
        return []

# ============ YAHOO FINANCE (fallback FIIs) ============
def fetch_yahoo_fii(ticker):
    """Busca dividendos de FIIs via yfinance (fallback se investidor10 falhar)."""
    try:
        tk = yf.Ticker(f"{ticker}.SA")
        divs_df = tk.dividends
        if divs_df is None or divs_df.empty:
            return []
        
        result = []
        for date, amount in divs_df.items():
            if amount > 0:
                dc = date.strftime("%Y-%m-%d")
                result.append({
                    "dc": dc,
                    "dp": estimate_payment_date(dc, 10),  # +10 dias úteis
                    "r": round(float(amount), 6),
                    "l": "RENDIMENTO"
                })
        return result
    except Exception as e:
        print(f"    Yahoo erro: {e}")
        return []

def parse_date_br(text):
    if not text:
        return None
    parts = text.strip().split("/")
    if len(parts) == 3:
        try:
            return f"{parts[2]}-{parts[1]}-{parts[0]}"
        except:
            return None
    return None

def estimate_payment_date(data_com, business_days=10):
    """Estima data de pagamento: data com + N dias úteis."""
    try:
        from datetime import timedelta
        dt = datetime.strptime(data_com, "%Y-%m-%d")
        count = 0
        while count < business_days:
            dt += timedelta(days=1)
            if dt.weekday() < 5:  # seg-sex
                count += 1
        return dt.strftime("%Y-%m-%d")
    except:
        return data_com

# ============ CALCULATIONS ============
def shares_at(operations, date):
    total = 0
    for op in operations:
        if op.get("d", "") <= date:
            total += op.get("q", 0)
    return max(0, total)

def process_dividends(all_events, operations):
    dy_known = {}
    monthly = {}
    filtered = {}
    
    for tk, events in all_events.items():
        ops = operations.get(tk, [])
        relevant = []
        ticker_total = 0
        
        for ev in events:
            cotas = shares_at(ops, ev["dc"])
            if cotas <= 0:
                continue
            valor = ev["r"] * cotas
            relevant.append(ev)
            ticker_total += valor
            month = (ev.get("dp") or ev["dc"])[:7]
            monthly[month] = monthly.get(month, 0) + valor
        
        if relevant:
            filtered[tk] = relevant
        if ticker_total > 0:
            dy_known[tk] = round(ticker_total, 2)
    
    monthly_list = [
        {"data": m, "total": round(v, 2)}
        for m, v in sorted(monthly.items())
        if v > 0.01
    ]
    return dy_known, monthly_list, filtered

# ============ MAIN ============
def main():
    print("=" * 60)
    print(f"Fetch Dividendos — {datetime.now().isoformat()}")
    print("=" * 60)
    
    db = init_firebase()
    
    ativos = db.collection("investimentos").document("ativos").get()
    if not ativos.exists:
        print("Nenhum ativo."); return
    
    lista = ativos.to_dict().get("lista", [])
    carteira = [a for a in lista if a.get("s") == "C"]
    
    # Também buscar Wishlist que têm operações (ativos vendidos com DY pendente)
    ops_doc = db.collection("investimentos").document("operacoes").get()
    operations = ops_doc.to_dict() if ops_doc.exists else {}
    
    wishlist_com_ops = [a for a in lista if a.get("s") == "W" and a["k"] in operations]
    todos = carteira + wishlist_com_ops
    
    acoes = [a["k"] for a in todos if a.get("tp") == "AÇÕES"]
    fiis = [a["k"] for a in todos if a.get("tp") == "FII"]
    etfs = [a["k"] for a in todos if a.get("tp") == "ETF"]
    
    print(f"Ações: {len(acoes)} (PlayInvest)")
    print(f"FIIs:  {len(fiis)} (Investidor10 → Yahoo fallback)")
    print(f"ETFs:  {len(etfs)} (Investidor10)")
    
    div_doc = db.collection("investimentos").document("dividendos").get()
    existing_dy = div_doc.to_dict().get("dy_known", {}) if div_doc.exists else {}
    
    all_events = {}
    
    # --- AÇÕES ---
    print(f"\n{'='*40}")
    print("AÇÕES — PlayInvest")
    print(f"{'='*40}")
    ok_a = 0
    for i, tk in enumerate(acoes):
        print(f"[{i+1}/{len(acoes)}] {tk}...", end="", flush=True)
        ev = fetch_playinvest(tk)
        if ev:
            all_events[tk] = ev
            ok_a += 1
            print(f" ✓ {len(ev)} eventos")
        else:
            print(f" ✗")
        time.sleep(DELAY)
    
    # --- FIIs (investidor10 primeiro, Yahoo fallback) ---
    print(f"\n{'='*40}")
    print("FIIs — Investidor10 → Yahoo Finance fallback")
    print(f"{'='*40}")
    ok_f = 0
    for i, tk in enumerate(fiis):
        print(f"[{i+1}/{len(fiis)}] {tk}...", end="", flush=True)
        # Tentar investidor10 primeiro (tem data com + data pagamento)
        ev = fetch_investidor10(tk, "fiis")
        if ev:
            all_events[tk] = ev
            ok_f += 1
            print(f" ✓ {len(ev)} eventos [inv10]")
        else:
            # Fallback: Yahoo Finance (+10 dias úteis estimados)
            ev = fetch_yahoo_fii(tk)
            if ev:
                all_events[tk] = ev
                ok_f += 1
                print(f" ✓ {len(ev)} eventos [yahoo]")
            else:
                print(f" ✗")
        time.sleep(DELAY)
    
    # --- ETFs (investidor10) ---
    print(f"\n{'='*40}")
    print("ETFs — Investidor10")
    print(f"{'='*40}")
    ok_e = 0
    for i, tk in enumerate(etfs):
        print(f"[{i+1}/{len(etfs)}] {tk}...", end="", flush=True)
        ev = fetch_investidor10(tk, "etfs")
        if ev:
            all_events[tk] = ev
            ok_e += 1
            print(f" ✓ {len(ev)} eventos")
        else:
            # Tentar como ação (alguns ETFs estão em /acoes/)
            ev = fetch_investidor10(tk, "acoes")
            if ev:
                all_events[tk] = ev
                ok_e += 1
                print(f" ✓ {len(ev)} eventos [acoes]")
            else:
                print(f" ✗")
        time.sleep(DELAY)
    
    print(f"\nFetch: Ações {ok_a}/{len(acoes)}, FIIs {ok_f}/{len(fiis)}, ETFs {ok_e}/{len(etfs)}")
    
    # --- Process ---
    print(f"\n{'='*40}")
    print("Calculando DY × sharesAt()...")
    print(f"{'='*40}")
    dy_known, monthly, filtered = process_dividends(all_events, operations)
    
    # Keep existing for failed tickers
    for tk, val in existing_dy.items():
        if tk not in dy_known:
            dy_known[tk] = val
    
    total = sum(dy_known.values())
    print(f"Total DY: R$ {total:.2f} ({len(dy_known)} tickers, {len(monthly)} meses)")
    
    # --- Save ---
    db.collection("investimentos").document("dividendos").set({
        "dy_known": dy_known,
        "mensal": monthly,
        "eventos": filtered,
        "ultimaAtualizacao": datetime.now().isoformat(),
        "fonte": "PlayInvest (ações) + Yahoo Finance (FIIs)"
    })
    print("✓ Firestore salvo!")
    
    # === FUNDAMENTAIS via yfinance — TODOS os tickers da B3 ===
    print(f"\n{'='*60}")
    print("Buscando fundamentos de TODOS os tickers B3 (yfinance)...")
    print(f"{'='*60}")
    
    # Lista abrangente: Ibovespa + SmallCaps + FIIs + ETFs
    B3_TICKERS = [
        # Ibovespa
        'ABEV3','ALPA4','ALOS3','ARZZ3','ASAI3','AZUL4','B3SA3','BBAS3','BBDC3','BBDC4',
        'BBSE3','BEEF3','BPAC11','BRAP4','BRFS3','BRKM5','CASH3','CCRO3','CIEL3','CMIG4',
        'CMIN3','COGN3','CPFE3','CPLE6','CRFB3','CSAN3','CSNA3','CVCB3','CYRE3','DXCO3',
        'ECOR3','EGIE3','ELET3','ELET6','EMBR3','ENEV3','ENGI11','EQTL3','EZTC3','FLRY3',
        'GGBR4','GOAU4','GOLL4','HAPV3','HYPE3','IGTI11','IRBR3','ITSA4','ITUB4','JBSS3',
        'KLBN11','KLBN4','LREN3','LWSA3','MGLU3','MRFG3','MRVE3','MULT3','NTCO3','PCAR3',
        'PETR3','PETR4','PETZ3','POSI3','PRIO3','QUAL3','RADL3','RAIL3','RAIZ4','RCSL3',
        'RDOR3','RENT3','RRRP3','SANB11','SBSP3','SLCE3','SMTO3','SOMA3','SUZB3','TAEE11',
        'TIMS3','TOTS3','UGPA3','USIM5','VALE3','VBBR3','VIVT3','WEGE3','YDUQ3',
        # SmallCaps populares
        'ABCB4','ALSO3','AURE3','BMGB4','BOAS3','BRSR6','CAML3','CBAV3','CEAB3','CGAS5',
        'CLSA3','CPLE3','CSMG3','DIRR3','DMMO3','ELMD3','ENBR3','EVEN3','FESA4','FIQE3',
        'GRND3','HBSA3','INTB3','ISAE4','JHSF3','KEPL3','LAVV3','LEVE3','LJQQ3','LOGG3',
        'MDIA3','MEGA3','MLAS3','MOVI3','MTRE3','MYPK3','NEOE3','ODPV3','OIBR3','PARD3',
        'PGMN3','PINE4','PLPL3','POMO4','PTBL3','RAPT4','RECV3','RNEW4','ROMI3','RSUL4',
        'SAPR11','SBFG3','SEER3','SIMH3','SMFT3','SOJA3','SQIA3','STBP3','TASA4','TGMA3',
        'TRIS3','TUPY3','UNIP6','VAMO3','VIIA3','VLID3','VULC3','WIZC3',
        # FIIs populares
        'BCFF11','BTLG11','CPTS11','DEVA11','GGRC11','HGBS11','HGCR11','HGLG11','HGRE11',
        'HGRU11','HSML11','IRDM11','JSRE11','KNCR11','KNIP11','KNRI11','LVBI11','MXRF11',
        'PVBI11','RBRF11','RBRR11','RECR11','RZAK11','RZAT11','RZTR11','SNEL11','TGAR11',
        'TRXF11','TVRI11','URPR11','VISC11','VGIP11','VILG11','XPLG11','XPML11',
        # ETFs
        'BOVA11','IVVB11','SMAL11','HASH11','GOLD11','SPXI11','DIVO11',
    ]
    
    # Adicionar tickers da carteira/wishlist que não estejam na lista
    for a in lista:
        tk = a.get("k","")
        if tk and tk not in B3_TICKERS and tk not in ('USDBRL','BTCBRL','ETHBRL'):
            B3_TICKERS.append(tk)
    
    B3_TICKERS = sorted(set(B3_TICKERS))
    print(f"Total: {len(B3_TICKERS)} tickers")
    
    fundamentos = {}
    existing_fund_doc = db.collection("investimentos").document("fundamentos").get()
    existing_fund = existing_fund_doc.to_dict() if existing_fund_doc.exists else {}
    
    ok_fund = 0
    for i, tk in enumerate(B3_TICKERS):
        if (i+1) % 20 == 0 or i == 0:
            print(f"[{i+1}/{len(B3_TICKERS)}]", end="", flush=True)
        try:
            ytk = yf.Ticker(f"{tk}.SA")
            info = ytk.info or {}
            
            pl = info.get('trailingPE') or info.get('forwardPE') or 0
            pvp = info.get('priceToBook') or 0
            dy = info.get('dividendYield') or info.get('trailingAnnualDividendYield') or 0
            lpa = info.get('trailingEps') or 0
            vpa = info.get('bookValue') or 0
            price = info.get('regularMarketPrice') or info.get('currentPrice') or 0
            name = info.get('longName') or info.get('shortName') or tk
            sector = info.get('sector') or info.get('industry') or ''
            
            if pl or pvp or price:
                fundamentos[tk] = {
                    'pl': round(pl, 2) if pl else 0,
                    'pvp': round(pvp, 2) if pvp else 0,
                    'dy': round(dy * 100, 2) if dy and dy < 1 else round(dy, 2) if dy else 0,
                    'lpa': round(lpa, 2) if lpa else 0,
                    'vpa': round(vpa, 2) if vpa else 0,
                    'price': round(price, 2) if price else 0,
                    'name': name,
                    'sector': sector
                }
                ok_fund += 1
        except Exception as e:
            pass
        time.sleep(0.8)
    
    print(f"\nFundamentos: {ok_fund}/{len(B3_TICKERS)} OK")
    
    fundamentos['ultimaAtualizacao'] = datetime.now().isoformat()
    
    # Firestore doc tem limite de 1MB — verificar tamanho
    import sys
    fund_size = sys.getsizeof(json.dumps(fundamentos))
    print(f"Tamanho: {fund_size/1024:.1f} KB")
    
    db.collection("investimentos").document("fundamentos").set(fundamentos)
    print(f"✓ Fundamentos salvos: {ok_fund} tickers")
    
    # --- Detail ---
    print(f"\n{'='*40}")
    print("Resumo Dividendos")
    print(f"{'='*40}")
    print(f"\n{'='*40}")
    for tk in sorted(dy_known.keys()):
        n = len(filtered.get(tk, []))
        src = "PI" if tk in acoes else "YF" if tk in fiis else "?"
        print(f"  {tk}: R$ {dy_known[tk]:.2f} ({n} ev) [{src}]")

if __name__ == "__main__":
    main()
