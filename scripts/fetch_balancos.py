"""
Busca balanços da CVM (DFP/ITR) e calcula indicadores fundamentalistas.
Salva no Firestore para uso no dashboard e Analista Graham.
"""
import os
import json
import time
import zipfile
import io
import requests
import pandas as pd
from datetime import datetime
import firebase_admin
from firebase_admin import credentials, firestore

CVM_BASE = "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC"
HEADERS = {"User-Agent": "Mozilla/5.0"}

def init_firebase():
    cred = credentials.Certificate(json.loads(os.environ["FIREBASE_CREDENTIALS"]))
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)
    return firestore.client()

def download_cvm_csv(doc_type, year):
    """Baixa e extrai CSV da CVM. doc_type: DFP ou ITR."""
    url = f"{CVM_BASE}/{doc_type}/DADOS/{doc_type.lower()}_cia_aberta_{year}.zip"
    print(f"  Baixando {url}...")
    try:
        resp = requests.get(url, headers=HEADERS, timeout=60)
        if resp.status_code != 200:
            print(f"  ✗ HTTP {resp.status_code}")
            return {}
        
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        dfs = {}
        for name in zf.namelist():
            if name.endswith('.csv'):
                # Extrair tipo: dfp_cia_aberta_DRE_con_2024.csv → DRE_CON
                parts = name.split('_cia_aberta_')
                if len(parts) > 1:
                    key = parts[1].replace('.csv', '').upper()
                    # Remover o ano do final: DRE_CON_2024 → DRE_CON
                    for y in ['_2024', '_2025', '_2026', '_2027']:
                        key = key.replace(y, '')
                else:
                    key = name.replace('.csv', '').upper()
                try:
                    df = pd.read_csv(zf.open(name), sep=';', encoding='latin-1', 
                                     dtype=str, on_bad_lines='skip')
                    dfs[key] = df
                    print(f"    {key}: {len(df)} linhas")
                except Exception as e:
                    print(f"    {key}: erro {e}")
        return dfs
    except Exception as e:
        print(f"  ✗ Erro: {e}")
        return {}

def extract_financials(dfs_by_year):
    """Extrai dados financeiros consolidados por empresa/ticker."""
    companies = {}  # {CNPJ: {nome, dados por periodo}}
    
    for year, dfs in dfs_by_year.items():
        # DRE - Demonstração de Resultado (consolidado primeiro, individual como fallback)
        dre_key = [k for k in dfs.keys() if 'DRE' in k and 'CON' in k]
        if not dre_key:
            dre_key = [k for k in dfs.keys() if 'DRE' in k]
        if dre_key:
            df = dfs[dre_key[0]]
            # Filtrar consolidado (ORDEM_EXERC=ÚLTIMO, GRUPO_DFP=DF Consolidado)
            if 'ORDEM_EXERC' in df.columns:
                df = df[df['ORDEM_EXERC'] == 'ÚLTIMO']
            if 'GRUPO_DFP' in df.columns:
                df_cons = df[df['GRUPO_DFP'].str.contains('Consolidado', na=False)]
                if len(df_cons) > 0:
                    df = df_cons
            
            for _, row in df.iterrows():
                cnpj = str(row.get('CNPJ_CIA', '')).strip()
                nome = str(row.get('DENOM_CIA', '')).strip()
                dt_ref = str(row.get('DT_REFER', '')).strip()
                conta = str(row.get('CD_CONTA', '')).strip()
                desc = str(row.get('DS_CONTA', '')).strip()
                try:
                    valor = float(str(row.get('VL_CONTA', '0')).replace(',', '.')) * 1000  # CVM em milhares
                except:
                    valor = 0
                
                if not cnpj or not dt_ref:
                    continue
                
                if cnpj not in companies:
                    companies[cnpj] = {'nome': nome, 'periodos': {}}
                
                if dt_ref not in companies[cnpj]['periodos']:
                    companies[cnpj]['periodos'][dt_ref] = {}
                
                # Mapear contas relevantes
                if conta == '3.01':
                    companies[cnpj]['periodos'][dt_ref]['receita'] = valor
                elif conta == '3.11':
                    companies[cnpj]['periodos'][dt_ref]['lucro_liquido'] = valor
                elif conta == '3.05':
                    companies[cnpj]['periodos'][dt_ref]['ebit'] = valor
        
        # BPP - Balanço Patrimonial Passivo (Patrimônio Líquido)
        bpp_key = [k for k in dfs.keys() if 'BPP' in k and 'CON' in k]
        if not bpp_key:
            bpp_key = [k for k in dfs.keys() if 'BPP' in k]
        if bpp_key:
            df = dfs[bpp_key[0]]
            if 'ORDEM_EXERC' in df.columns:
                df = df[df['ORDEM_EXERC'] == 'ÚLTIMO']
            if 'GRUPO_DFP' in df.columns:
                df_cons = df[df['GRUPO_DFP'].str.contains('Consolidado', na=False)]
                if len(df_cons) > 0:
                    df = df_cons
            
            for _, row in df.iterrows():
                cnpj = str(row.get('CNPJ_CIA', '')).strip()
                dt_ref = str(row.get('DT_REFER', '')).strip()
                conta = str(row.get('CD_CONTA', '')).strip()
                try:
                    valor = float(str(row.get('VL_CONTA', '0')).replace(',', '.')) * 1000
                except:
                    valor = 0
                
                if not cnpj or not dt_ref:
                    continue
                if cnpj not in companies:
                    continue
                if dt_ref not in companies[cnpj]['periodos']:
                    companies[cnpj]['periodos'][dt_ref] = {}
                
                if conta == '2.03':  # Patrimônio Líquido Consolidado
                    companies[cnpj]['periodos'][dt_ref]['patrimonio_liquido'] = valor
                elif conta == '2.01':  # Passivo Circulante
                    companies[cnpj]['periodos'][dt_ref]['passivo_circulante'] = valor
                elif conta == '2.02':  # Passivo Não Circulante
                    companies[cnpj]['periodos'][dt_ref]['passivo_nao_circulante'] = valor
        
        # BPA - Balanço Patrimonial Ativo
        bpa_key = [k for k in dfs.keys() if 'BPA' in k and 'CON' in k]
        if not bpa_key:
            bpa_key = [k for k in dfs.keys() if 'BPA' in k]
        if bpa_key:
            df = dfs[bpa_key[0]]
            if 'ORDEM_EXERC' in df.columns:
                df = df[df['ORDEM_EXERC'] == 'ÚLTIMO']
            if 'GRUPO_DFP' in df.columns:
                df_cons = df[df['GRUPO_DFP'].str.contains('Consolidado', na=False)]
                if len(df_cons) > 0:
                    df = df_cons
            
            for _, row in df.iterrows():
                cnpj = str(row.get('CNPJ_CIA', '')).strip()
                dt_ref = str(row.get('DT_REFER', '')).strip()
                conta = str(row.get('CD_CONTA', '')).strip()
                try:
                    valor = float(str(row.get('VL_CONTA', '0')).replace(',', '.')) * 1000
                except:
                    valor = 0
                
                if not cnpj or cnpj not in companies:
                    continue
                if dt_ref not in companies[cnpj].get('periodos', {}):
                    continue
                
                if conta == '1':  # Ativo Total
                    companies[cnpj]['periodos'][dt_ref]['ativo_total'] = valor
                elif conta == '1.01':  # Ativo Circulante
                    companies[cnpj]['periodos'][dt_ref]['ativo_circulante'] = valor
    
    return companies

def calculate_metrics(companies, ticker_map):
    """Calcula indicadores fundamentalistas por ticker."""
    results = {}
    
    for cnpj, data in companies.items():
        ticker = ticker_map.get(cnpj)
        if not ticker:
            continue
        
        periodos = sorted(data['periodos'].keys(), reverse=True)
        if not periodos:
            continue
        
        metrics = {
            'nome': data['nome'],
            'periodos_disponiveis': periodos[:8],
            'historico': []
        }
        
        for dt in periodos[:8]:  # Últimos 8 períodos
            p = data['periodos'][dt]
            rec = p.get('receita', 0)
            ll = p.get('lucro_liquido', 0)
            pl_val = p.get('patrimonio_liquido', 0)
            ebit = p.get('ebit', 0)
            at = p.get('ativo_total', 0)
            pc = p.get('passivo_circulante', 0)
            pnc = p.get('passivo_nao_circulante', 0)
            
            entry = {
                'periodo': dt,
                'receita': round(rec / 1e6, 2),  # Em milhões
                'lucro_liquido': round(ll / 1e6, 2),
                'patrimonio_liquido': round(pl_val / 1e6, 2),
                'ebit': round(ebit / 1e6, 2),
                'ativo_total': round(at / 1e6, 2),
                'divida_bruta': round((pc + pnc) / 1e6, 2),
                'margem_liquida': round(ll / rec * 100, 2) if rec else 0,
                'roe': round(ll / pl_val * 100, 2) if pl_val else 0,
                'divida_pl': round((pc + pnc) / pl_val, 2) if pl_val else 0,
                'margem_ebit': round(ebit / rec * 100, 2) if rec else 0,
            }
            metrics['historico'].append(entry)
        
        # Calcular crescimento
        if len(metrics['historico']) >= 2:
            h = metrics['historico']
            rec_atual = h[0]['receita']
            rec_ant = h[1]['receita']
            ll_atual = h[0]['lucro_liquido']
            ll_ant = h[1]['lucro_liquido']
            
            metrics['cresc_receita'] = round((rec_atual - rec_ant) / abs(rec_ant) * 100, 2) if rec_ant else 0
            metrics['cresc_lucro'] = round((ll_atual - ll_ant) / abs(ll_ant) * 100, 2) if ll_ant else 0
        
        # Último período como resumo
        if metrics['historico']:
            last = metrics['historico'][0]
            metrics['ultimo_periodo'] = last['periodo']
            metrics['margem_liquida'] = last['margem_liquida']
            metrics['roe'] = last['roe']
            metrics['divida_pl'] = last['divida_pl']
            metrics['receita_mm'] = last['receita']
            metrics['lucro_mm'] = last['lucro_liquido']
        
        results[ticker] = metrics
    
    return results

def normalize_name(name):
    """Remove sufixos, pontuação e normaliza nome de empresa."""
    import re
    n = name.upper().strip()
    # Remover sufixos jurídicos
    for suf in [' S.A.', ' S/A', ' SA', ' S.A', ' LTDA', ' LTDA.', ' - EM RECUPERACAO JUDICIAL',
                ' - EM REC. JUDICIAL', ' CIA.', ' CIA', ' HOLDING', ' PARTICIPACOES', ' PART.']:
        n = n.replace(suf, '')
    n = re.sub(r'[.\-/,()]', ' ', n)
    n = re.sub(r'\s+', ' ', n).strip()
    return n

def build_ticker_map(companies):
    """Mapeia CNPJ → ticker usando cadastro CVM + yfinance + fuzzy matching."""
    
    # 1. Carregar cadastro CVM
    cad_names = {}  # CNPJ prefix → nome comercial
    try:
        print("  Baixando cadastro CVM...")
        cad_url = "https://dados.cvm.gov.br/dados/CIA_ABERTA/CAD/DADOS/cad_cia_aberta.csv"
        cad_df = pd.read_csv(cad_url, sep=';', encoding='latin-1', dtype=str)
        for _, row in cad_df.iterrows():
            cnpj = str(row.get('CNPJ_CIA', '')).strip()
            sit = str(row.get('SIT_REG', '')).strip()
            if cnpj and sit == 'ATIVO':
                social = normalize_name(str(row.get('DENOM_SOCIAL', '')))
                comercial = normalize_name(str(row.get('DENOM_COMERC', '')))
                cad_names[cnpj[:10]] = {'social': social, 'comercial': comercial}
        print(f"  Cadastro CVM: {len(cad_names)} empresas ativas")
    except Exception as e:
        print(f"  Erro cadastro: {e}")
    
    # 2. Carregar nomes do yfinance (Firestore fundamentos)
    fund_names = {}  # ticker → nome normalizado
    try:
        fund_doc = db.collection("investimentos").document("fundamentos").get()
        if fund_doc.exists:
            for tk, data in fund_doc.to_dict().items():
                if isinstance(data, dict) and data.get('name'):
                    fund_names[tk] = normalize_name(data['name'])
        print(f"  Fundamentos yfinance: {len(fund_names)} tickers")
    except:
        pass
    
    # 3. Mapa manual CNPJ prefix → ticker
    CNPJ_MAP = {
        '33.000.167': 'PETR4', '00.000.000': 'BBAS3', '60.746.948': 'BBDC4',
        '60.872.504': 'ITUB4', '61.532.644': 'ITSA4', '33.592.510': 'VALE3',
        '84.429.695': 'WEGE3', '02.916.265': 'B3SA3', '33.611.500': 'ELET3',
        '20.706.413': 'CMIG4', '76.535.764': 'CPLE6', '47.960.950': 'ABEV3',
        '89.850.341': 'BRAP4', '02.558.157': 'SUZB3', '42.150.391': 'JBSS3',
        '02.919.555': 'PRIO3', '76.484.013': 'EZTC3', '08.534.605': 'HYPE3',
        '61.585.865': 'CSNA3', '33.041.260': 'VBBR3', '07.526.557': 'RENT3',
        '02.328.280': 'CCRO3', '04.088.208': 'SBSP3', '60.894.730': 'SANB11',
        '92.754.738': 'SLCE3', '87.456.562': 'ALOS3', '02.474.103': 'TIMS3',
        '02.429.144': 'VIVT3', '60.850.229': 'LREN3', '47.508.411': 'HAPV3',
        '02.800.026': 'RDOR3', '01.532.247': 'PCAR3', '13.574.594': 'MGLU3',
        '08.181.508': 'FLRY3', '04.310.392': 'MULT3', '02.388.668': 'ENEV3',
        '03.220.438': 'EQTL3', '15.527.906': 'ENGI11', '06.981.180': 'CMIN3',
        '14.011.962': 'AZUL4', '07.689.002': 'RAIL3', '34.274.233': 'BRFS3',
        '23.637.428': 'CRFB3', '61.412.110': 'RADL3', '08.886.098': 'ASAI3',
        '73.178.600': 'CYRE3', '07.628.528': 'COGN3', '04.065.791': 'CPFE3',
        '43.776.517': 'IGTI11', '04.423.567': 'UGPA3', '23.865.067': 'YDUQ3',
        '60.148.154': 'ECOR3', '16.404.287': 'DXCO3', '02.762.115': 'MRVE3',
        '97.837.181': 'GOAU4', '33.613.286': 'GGBR4', '07.170.938': 'CASH3',
        '08.063.988': 'LWSA3', '10.346.185': 'CVCB3', '91.826.310': 'SMTO3',
        '89.637.490': 'BRKM5', '04.184.779': 'CSAN3', '07.516.534': 'BEEF3',
        '09.346.601': 'RRRP3', '06.980.064': 'MRFG3', '11.237.486': 'ALPA4',
        '02.302.101': 'IRBR3', '76.487.032': 'KLBN11', '56.720.428': 'NTCO3',
        '03.847.461': 'GOLL4', '28.757.546': 'TOTS3', '50.746.577': 'TAEE11',
        '82.508.433': 'FESA4', '79.739.440': 'MYPK3', '44.990.901': 'ROMI3',
        '88.610.126': 'GRND3', '81.423.838': 'POMO4', '12.644.536': 'SOJA3',
        '07.318.783': 'ABCB4', '92.702.067': 'BRSR6', '15.141.799': 'CAML3',
        '30.306.294': 'BMGB4', '33.014.556': 'ISAE4', '61.856.571': 'EGIE3',
        '86.375.425': 'MTRE3', '80.659.624': 'TRIS3', '14.810.028': 'FIQE3',
        '60.651.809': 'CIEL3', '10.347.985': 'SOMA3', '04.738.023': 'USIM5',
        '47.427.653': 'BBSE3', '76.622.217': 'SBFG3', '92.660.007': 'PETZ3',
        '89.073.104': 'KLBN4',
    }
    
    # 4. Mapear: CNPJ manual → nome CVM vs yfinance → fuzzy
    ticker_map = {}
    mapped_tickers = set()
    
    for cnpj, data in companies.items():
        cnpj_prefix = cnpj[:10] if len(cnpj) >= 10 else cnpj
        nome_cvm = normalize_name(data['nome'])
        
        # Passo 1: CNPJ manual direto
        for prefix, ticker in CNPJ_MAP.items():
            if cnpj.startswith(prefix) and ticker not in mapped_tickers:
                ticker_map[cnpj] = ticker
                mapped_tickers.add(ticker)
                break
        if cnpj in ticker_map:
            continue
        
        # Passo 2: Cruzar nome CVM com nome yfinance
        best_match = None
        best_score = 0
        for tk, yname in fund_names.items():
            if tk in mapped_tickers:
                continue
            # Match exato
            if nome_cvm == yname:
                best_match = tk
                best_score = 100
                break
            # Match parcial: primeiras palavras
            cvm_words = nome_cvm.split()
            y_words = yname.split()
            if len(cvm_words) >= 1 and len(y_words) >= 1:
                # Primeira palavra igual (ex: PETROBRAS = PETROBRAS)
                if cvm_words[0] == y_words[0] and len(cvm_words[0]) >= 4:
                    score = 80
                    # Bonus se 2+ palavras batem
                    common = set(cvm_words) & set(y_words)
                    score += len(common) * 5
                    if score > best_score:
                        best_score = score
                        best_match = tk
                # Nome contido no outro
                elif nome_cvm in yname or yname in nome_cvm:
                    score = 70
                    if score > best_score:
                        best_score = score
                        best_match = tk
        
        if best_match and best_score >= 70:
            ticker_map[cnpj] = best_match
            mapped_tickers.add(best_match)
            continue
        
        # Passo 3: Match pelo cadastro CVM (nome comercial)
        cad = cad_names.get(cnpj_prefix)
        if cad:
            for tk, yname in fund_names.items():
                if tk in mapped_tickers:
                    continue
                comercial = cad.get('comercial', '')
                if comercial and len(comercial) >= 3:
                    if comercial in yname or yname in comercial:
                        ticker_map[cnpj] = tk
                        mapped_tickers.add(tk)
                        break
                    # Primeira palavra
                    c_words = comercial.split()
                    y_words = yname.split()
                    if c_words and y_words and c_words[0] == y_words[0] and len(c_words[0]) >= 4:
                        ticker_map[cnpj] = tk
                        mapped_tickers.add(tk)
                        break
    
    print(f"  Mapeados: {len(ticker_map)} (CNPJ: {sum(1 for c in ticker_map if c[:10] in [p.replace('.','') for p in CNPJ_MAP])}, nome: {len(ticker_map)-sum(1 for c in ticker_map if c[:10] in [p.replace('.','') for p in CNPJ_MAP])})")
    return ticker_map

def main():
    print("=" * 60)
    print(f"CVM Balanços — {datetime.now().isoformat()}")
    print("=" * 60)
    
    db = init_firebase()
    
    # Baixar dados dos últimos 3 anos
    current_year = datetime.now().year
    years = [current_year, current_year - 1, current_year - 2]
    
    all_dfs = {}
    for year in years:
        print(f"\n--- DFP {year} ---")
        dfs = download_cvm_csv("DFP", year)
        if dfs:
            all_dfs[f"DFP_{year}"] = dfs
        
        print(f"\n--- ITR {year} ---")
        dfs = download_cvm_csv("ITR", year)
        if dfs:
            all_dfs[f"ITR_{year}"] = dfs
    
    if not all_dfs:
        print("Nenhum dado obtido da CVM")
        return
    
    # Extrair dados financeiros
    print(f"\n{'='*40}")
    print("Processando balanços...")
    companies = extract_financials(all_dfs)
    print(f"Empresas encontradas: {len(companies)}")
    
    # Mapear CNPJ → ticker usando cadastro CVM + mapa manual expandido
    ticker_map = build_ticker_map(companies)
    print(f"Tickers mapeados: {len(ticker_map)}")
    
    print(f"Tickers mapeados: {len(ticker_map)}")
    
    # Calcular métricas
    results = calculate_metrics(companies, ticker_map)
    print(f"Análises geradas: {len(results)}")
    
    # Salvar no Firestore
    if results:
        results['ultimaAtualizacao'] = datetime.now().isoformat()
        db.collection("investimentos").document("balancos").set(results)
        print(f"✓ Salvo no Firestore: {len(results)-1} empresas")
        
        # Resumo
        for tk in sorted(results.keys()):
            if tk == 'ultimaAtualizacao':
                continue
            r = results[tk]
            print(f"  {tk}: ROE={r.get('roe',0)}% ML={r.get('margem_liquida',0)}% D/PL={r.get('divida_pl',0)} Rec={r.get('receita_mm',0)}MM")

if __name__ == "__main__":
    main()
