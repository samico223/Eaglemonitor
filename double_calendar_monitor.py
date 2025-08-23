# double_calendar_monitor.py

import streamlit as st
import pandas as pd
import requests
from datetime import datetime, timedelta
import json
import time
import asyncio
import telegram

# ==============================================================================
# CONFIGURAÇÕES GERAIS E SEGREDOS
# ==============================================================================
st.set_page_config(page_title="Monitor de Calendários", layout="wide")
MARKET_DATA_TOKEN = st.secrets.get("MARKET_DATA_TOKEN", "")
BOT_TOKEN = st.secrets.get("telegram", {}).get("BOT_TOKEN", "")
CHAT_ID = st.secrets.get("telegram", {}).get("CHAT_ID", "")
API_BASE_URL = "https://api.marketdata.app/v1/"
REFRESH_INTERVAL_SECONDS = 300 
DB_FILE_PATH = 'calendars_db.json'

# ==============================================================================
# FUNÇÕES DE PERSISTÊNCIA
# ==============================================================================
def load_positions():
    try:
        with open(DB_FILE_PATH, 'r') as f: return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError): return {}

def save_positions(positions_dict):
    with open(DB_FILE_PATH, 'w') as f: json.dump(positions_dict, f, indent=4)

# ==============================================================================
# FUNÇÕES DE API, CÁLCULOS E ALERTAS
# ==============================================================================
def send_telegram_message(message):
    async def send():
        try:
            bot = telegram.Bot(token=BOT_TOKEN)
            await bot.send_message(chat_id=CHAT_ID, text=message, parse_mode='Markdown')
        except Exception as e: st.error(f"Falha ao enviar Telegram: {e}")
    try: asyncio.run(send())
    except RuntimeError: asyncio.get_running_loop().create_task(send())

@st.cache_data(ttl=REFRESH_INTERVAL_SECONDS - 10)
def get_option_data(option_symbol):
    if not MARKET_DATA_TOKEN or not option_symbol: return None
    url = f"{API_BASE_URL}options/quotes/{option_symbol}/"
    params = {'token': MARKET_DATA_TOKEN}
    try:
        r = requests.get(url, params=params, headers={"Accept": "application/json"})
        r.raise_for_status()
        data = r.json()
        return data if data.get('s') == 'ok' else None
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 400: st.toast(f"Erro 400: Símbolo inválido: {option_symbol}", icon="🚨")
        else: st.toast(f"Erro de API para {option_symbol}: {e}", icon="🚨")
        return None
    except requests.exceptions.RequestException as e:
        st.toast(f"Erro de conexão para {option_symbol}: {e}", icon="🚨")
        return None

def generate_option_symbol(ticker, exp_date, strike, option_type):
    exp_dt = datetime.strptime(exp_date, "%Y-%m-%d")
    strike_part = f"{int(strike * 1000):08d}"
    base_ticker = ''.join([i for i in ticker if not i.isdigit()])
    return f"{base_ticker}{exp_dt.strftime('%y%m%d')}{option_type.upper()}{strike_part}"

def calculate_z_percent(td_price_back, td_price_front, now_price_back, now_price_front):
    initial_cost = td_price_back - td_price_front
    if initial_cost == 0: return 0.0
    current_value = now_price_back - now_price_front
    profit_loss = current_value - initial_cost
    return (profit_loss / abs(initial_cost)) * 100

# ==============================================================================
# NOVO: FUNÇÃO REUTILIZÁVEL PARA RENDERIZAR UM CALENDÁRIO
# ==============================================================================
def render_calendar_block(ticker, calendar_data, calendar_id, history_data):
    """
    Desenha um bloco de monitoramento completo para um único calendário.
    `calendar_id` é a chave única para o histórico (ex: "put_original", "adj_0").
    """
    cal_type = calendar_data['type'].upper()
    expirations = calendar_data['expirations']

    st.subheader(f"Calendário {calendar_data['display_name']}")
    
    front_symbol = generate_option_symbol(ticker, expirations['front'], calendar_data['strike'], cal_type)
    back_symbol = generate_option_symbol(ticker, expirations['back'], calendar_data['strike'], cal_type)
    
    front_data = get_option_data(front_symbol)
    back_data = get_option_data(back_symbol)
    
    now_price_front = front_data['last'][0] if front_data and front_data.get('last') else 0
    now_price_back = back_data['last'][0] if back_data and back_data.get('last') else 0
    
    z_percent = calculate_z_percent(calendar_data['td_price_back'], calendar_data['td_price_front'], now_price_back, now_price_front)
    
    # Adiciona o Z atual ao histórico correto
    history_data['calendars'].setdefault(calendar_id, []).append(z_percent)
    
    # Lógica de Alerta
    if calendar_data.get('alert_target', 0) > 0:
        if z_percent >= calendar_data['alert_target'] and not calendar_data.get('alert_sent', False):
            msg = f"🎯 *ALERTA DE LUCRO ({cal_type})* 🎯\n\n*Ativo:* `{ticker}`\n*Calendário:* {cal_type} Strike {calendar_data['strike']:.2f}\n*Lucro Atual:* `{z_percent:.2f}%`\n*Meta:* `{calendar_data['alert_target']:.2f}%`"
            send_telegram_message(msg)
            calendar_data['alert_sent'] = True
        elif z_percent < calendar_data['alert_target'] and calendar_data.get('alert_sent', False):
            calendar_data['alert_sent'] = False
            
    # Exibição
    col1, col2 = st.columns(2)
    front_label, back_label = (f"{cal_type}F Now", f"{cal_type}B Now")
    col1.metric(front_label, f"{now_price_front:.2f}", f"↑ TD: {calendar_data['td_price_front']:.2f}")
    col2.metric(back_label, f"{now_price_back:.2f}", f"↑ TD: {calendar_data['td_price_back']:.2f}")
    
    st.metric(f"%Z (Alvo: {calendar_data.get('alert_target', 0)}%)", f"{z_percent:.2f}%")
    
    if len(history_data['calendars'][calendar_id]) > 1:
        chart_data = pd.DataFrame({f"%Z {calendar_data['display_name']}": history_data['calendars'][calendar_id]}, index=history_data['timestamp'])
        st.line_chart(chart_data)

    st.divider()

    return back_data # Retorna dados da perna longa para cálculo da VOL

# ==============================================================================
# CORPO PRINCIPAL DO APP
# ==============================================================================
st.title("🗓️ Monitoramento de Calendários Duplos Pré-Earnings")

if 'positions' not in st.session_state:
    st.session_state.positions = load_positions()

# Formulário para adicionar NOVA POSIÇÃO
with st.sidebar:
    st.header("Adicionar Nova Posição")
    with st.form(key="add_position_form", clear_on_submit=True):
        # ... (Campos do formulário para nova posição) ...
        ticker = st.text_input("Ticker do Ativo (ex: PETR4)").upper()
        st.subheader("Calendário PUT")
        put_strike = st.number_input("Strike da PUT", format="%.2f", step=0.01, key="p_s")
        td_price_pf = st.number_input("Preço TD - Put Front (Venda)", format="%.2f", step=0.01, key="p_pf")
        td_price_pb = st.number_input("Preço TD - Put Back (Compra)", format="%.2f", step=0.01, key="p_pb")
        put_alert_target = st.number_input("Alerta de Lucro % (PUT)", min_value=0.0, step=1.0, key="p_alert")
        st.subheader("Calendário CALL")
        call_strike = st.number_input("Strike da CALL", format="%.2f", step=0.01, key="c_s")
        td_price_cf = st.number_input("Preço TD - Call Front (Venda)", format="%.2f", step=0.01, key="c_cf")
        td_price_cb = st.number_input("Preço TD - Call Back (Compra)", format="%.2f", step=0.01, key="c_cb")
        call_alert_target = st.number_input("Alerta de Lucro % (CALL)", min_value=0.0, step=1.0, key="c_alert")
        st.subheader("Dados Gerais da Operação")
        front_exp = st.date_input("Vencimento Front (Curto)")
        back_exp = st.date_input("Vencimento Back (Longo)")
        td_back_vol = st.number_input("Volatilidade Back (TD %)", min_value=0.0, step=0.1, format="%.2f")
        
        submitted = st.form_submit_button("Adicionar Posição")

        if submitted and ticker:
            # Lógica para criar a estrutura da nova posição
            front_exp_str = front_exp.strftime("%Y-%m-%d")
            back_exp_str = back_exp.strftime("%Y-%m-%d")
            fad_date = front_exp - timedelta(days=14)
            
            new_pos = {
                "put_original": {"type": "p", "display_name": "PUT Original", "strike": put_strike, "td_price_front": td_price_pf, "td_price_back": td_price_pb, "alert_target": put_alert_target, "alert_sent": False, "expirations": {"front": front_exp_str, "back": back_exp_str}},
                "call_original": {"type": "c", "display_name": "CALL Original", "strike": call_strike, "td_price_front": td_price_cf, "td_price_back": td_price_cb, "alert_target": call_alert_target, "alert_sent": False, "expirations": {"front": front_exp_str, "back": back_exp_str}},
                "fad_date": fad_date.strftime("%Y-%m-%d"),
                "td_back_vol": td_back_vol,
                "history": {"timestamp": [], "calendars": {}, "back_vol": []}, 
                "adjustments": []
            }
            st.session_state.positions[ticker] = new_pos
            save_positions(st.session_state.positions)
            st.success(f"Posição em {ticker} adicionada!")
            st.rerun()

# LÓGICA DE EXIBIÇÃO PRINCIPAL REATORADA
if not st.session_state.positions:
    st.info("Nenhuma posição monitorada. Adicione uma na barra lateral.")
else:
    # Atualiza o timestamp para o histórico de todas as posições
    current_time = datetime.now()
    for data in st.session_state.positions.values():
        if not data['history']['timestamp'] or data['history']['timestamp'][-1] != current_time.strftime("%H:%M"):
            data['history']['timestamp'].append(current_time.strftime("%H:%M"))

    for ticker, data in list(st.session_state.positions.items()):
        with st.expander(f"Ativo: {ticker}", expanded=True):
            
            # Renderiza os calendários originais e de ajuste
            # O layout de 2 colunas organiza os calendários lado a lado
            col1, col2 = st.columns(2)
            
            with col1:
                back_data_p = render_calendar_block(ticker, data['put_original'], 'put_original', data['history'])
            
            with col2:
                back_data_c = render_calendar_block(ticker, data['call_original'], 'call_original', data['history'])

            # Renderiza os ajustes
            for i, adj_data in enumerate(data.get('adjustments', [])):
                col1, col2 = st.columns(2) # Cria novas colunas para cada par de ajustes
                if i % 2 == 0:
                    with col1:
                        render_calendar_block(ticker, adj_data, f'adj_{i}', data['history'])
                else:
                    with col2:
                        render_calendar_block(ticker, adj_data, f'adj_{i}', data['history'])

            # Painel de controle geral da posição (Vol, FAD, etc.)
            st.subheader("Controle Geral da Posição")

            # Cálculo da Vol Média das Pernas Longas
            back_vol_now_p = back_data_p['iv'][0] if back_data_p and back_data_p.get('iv') else 0
            back_vol_now_c = back_data_c['iv'][0] if back_data_c and back_data_c.get('iv') else 0
            back_vol_now = ((back_vol_now_p + back_vol_now_c) / 2) * 100 if back_vol_now_p and back_vol_now_c else 0
            data['history']['back_vol'].append(back_vol_now)
            
            td_vol = data.get("td_back_vol", 0)
            st.metric("Vol Média Atual (Back)", f"{back_vol_now:.2f}%", f"↑ TD: {td_vol:.2f}%")
            if len(data['history']['back_vol']) > 1:
                chart_data_v = pd.DataFrame({'Back Vol': data['history']['back_vol']}, index=data['history']['timestamp'])
                st.line_chart(chart_data_v)
            
            fad_dt = datetime.strptime(data['fad_date'], "%Y-%m-%d").date()
            dias_para_fad = (fad_dt - datetime.now().date()).days
            st.info(f"**FAD (Final Adjustment Date):** {fad_dt.strftime('%d/%m/%Y')} (Faltam {dias_para_fad} dias)")

            # Lógica para abrir o formulário de ajuste
            if st.button("➕ Adicionar Ajuste", key=f"add_adj_{ticker}"):
                st.session_state.adjusting_ticker = ticker
                st.rerun()
            
            if st.button("❌ Excluir Posição", key=f"del_{ticker}"):
                del st.session_state.positions[ticker]
                if 'adjusting_ticker' in st.session_state: del st.session_state.adjusting_ticker
                save_positions(st.session_state.positions)
                st.rerun()

# NOVO: FORMULÁRIO DE AJUSTE (aparece condicionalmente)
if 'adjusting_ticker' in st.session_state:
    ticker_to_adjust = st.session_state.adjusting_ticker
    with st.form(key="adjustment_form"):
        st.header(f"Adicionar Ajuste para {ticker_to_adjust}")
        
        adj_type = st.selectbox("Tipo de Calendário", ["put", "call"])
        adj_strike = st.number_input("Strike do Ajuste", format="%.2f", step=0.01)
        adj_td_price_front = st.number_input("Preço TD - Front (Venda)", format="%.2f", step=0.01)
        adj_td_price_back = st.number_input("Preço TD - Back (Compra)", format="%.2f", step=0.01)
        adj_alert_target = st.number_input("Alerta de Lucro %", min_value=0.0, step=1.0)
        adj_front_exp = st.date_input("Vencimento Front (Ajuste)")
        adj_back_exp = st.date_input("Vencimento Back (Ajuste)")

        save_adj_button = st.form_submit_button("Salvar Ajuste")

        if save_adj_button:
            num_adjustments = len(st.session_state.positions[ticker_to_adjust].get('adjustments', []))
            adj_exp_str = {"front": adj_front_exp.strftime("%Y-%m-%d"), "back": adj_back_exp.strftime("%Y-%m-%d")}
            
            new_adj = {
                "type": adj_type,
                "display_name": f"{adj_type.upper()} Ajuste {num_adjustments + 1}",
                "strike": adj_strike,
                "td_price_front": adj_td_price_front,
                "td_price_back": adj_td_price_back,
                "alert_target": adj_alert_target,
                "alert_sent": False,
                "expirations": adj_exp_str
            }
            
            st.session_state.positions[ticker_to_adjust].setdefault('adjustments', []).append(new_adj)
            del st.session_state.adjusting_ticker
            save_positions(st.session_state.positions)
            st.rerun()

    if st.button("Cancelar Ajuste"):
        del st.session_state.adjusting_ticker
        st.rerun()

# SALVAMENTO E ATUALIZAÇÃO
save_positions(st.session_state.positions)
st.caption(f"Última atualização: {datetime.now().strftime('%H:%M:%S')}")
time.sleep(REFRESH_INTERVAL_SECONDS)
st.rerun()
