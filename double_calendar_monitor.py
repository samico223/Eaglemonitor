# double_calendar_monitor.py

import streamlit as st
import pandas as pd
import requests
from datetime import datetime, timedelta
import json
import time
import asyncio
import telegram
from supabase import create_client, Client

# ==============================================================================
# CONFIGURAÇÕES GERAIS E SEGREDOS
# ==============================================================================
st.set_page_config(page_title="Monitor de Calendários", layout="wide")

st.markdown("""
    <style>
    h1 {font-size: 2.2rem;}
    h3 {font-size: 1.1rem;}
    div[data-testid="stMetricValue"] {font-size: 1.75rem;}
    div[data-testid="stMetricLabel"] {font-size: 0.9rem;}
    div[data-testid="stMetricDelta"] {font-size: 0.9rem;}
    </style>
    """, unsafe_allow_html=True)

MARKET_DATA_TOKEN = st.secrets.get("MARKET_DATA_TOKEN", "")
BOT_TOKEN = st.secrets.get("telegram", {}).get("BOT_TOKEN", "")
CHAT_ID = st.secrets.get("telegram", {}).get("CHAT_ID", "")
API_BASE_URL = "https://api.marketdata.app/v1/"
REFRESH_INTERVAL_SECONDS = 300 

try:
    supabase_url = st.secrets["supabase"]["url"]
    supabase_key = st.secrets["supabase"]["key"]
    supabase: Client = create_client(supabase_url, supabase_key)
except Exception as e:
    st.error(f"Erro ao conectar com o Supabase. Verifique os 'Secrets'. Detalhes: {e}")
    st.stop()

# ==============================================================================
# FUNÇÕES DE BANCO DE DADOS
# ==============================================================================
def load_positions_from_db():
    try:
        response = supabase.table('positions').select('ticker, position_data').execute()
        return {item['ticker']: item['position_data'] for item in response.data}
    except Exception as e:
        st.error(f"Erro ao carregar posições do DB: {e}")
        return {}

def add_position_to_db(ticker, position_data):
    try:
        supabase.table('positions').insert({"ticker": ticker, "position_data": position_data}).execute()
    except Exception as e:
        st.error(f"Erro ao adicionar posição no DB: {e}")

def update_position_in_db(ticker, position_data):
    try:
        supabase.table('positions').update({"position_data": position_data}).eq('ticker', ticker).execute()
    except Exception as e:
        st.error(f"Erro ao atualizar posição no DB: {e}")

def delete_position_from_db(ticker):
    try:
        supabase.table('positions').delete().eq('ticker', ticker).execute()
    except Exception as e:
        st.error(f"Erro ao deletar posição no DB: {e}")

# ==============================================================================
# FUNÇÕES DE API E CÁLCULOS
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
    if not MARKET_DATA_TOKEN or not option_symbol: return (False, "Token ou símbolo ausente.")
    url = f"{API_BASE_URL}options/quotes/{option_symbol}/"
    params = {'token': MARKET_DATA_TOKEN}
    try:
        r = requests.get(url, params=params, headers={"Accept": "application/json"})
        r.raise_for_status()
        data = r.json()
        if data.get('s') == 'ok': return (True, data)
        else: return (False, f"API retornou 'no_data' para {option_symbol}.")
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 400: return (False, f"Erro 400: Símbolo inválido: {option_symbol}")
        else: return (False, f"Erro de API para {option_symbol}: {e}")
    except requests.exceptions.RequestException as e:
        return (False, f"Erro de conexão para {option_symbol}: {e}")

def generate_option_symbol(ticker, exp_date, strike, option_type):
    exp_dt = datetime.strptime(exp_date, "%Y-%m-%d")
    strike_part = f"{int(strike * 1000):08d}"
    base_ticker = ''.join([i for i in ticker if not i.isdigit()])
    return f"{base_ticker}{exp_dt.strftime('%y%m%d')}{option_type[0].upper()}{strike_part}"

def calculate_pl_values(td_price_back, td_price_front, now_price_back, now_price_front):
    initial_cost = td_price_back - td_price_front
    if initial_cost == 0: return {"initial_cost": 0, "absolute_pl": 0, "z_percent": 0}
    current_value = now_price_back - now_price_front
    absolute_pl = current_value - initial_cost
    z_percent = (absolute_pl / abs(initial_cost)) * 100
    return {"initial_cost": initial_cost, "absolute_pl": absolute_pl, "z_percent": z_percent}

# ==============================================================================
# FUNÇÃO DE RENDERIZAÇÃO
# ==============================================================================
def render_calendar_block(ticker, calendar_data, live_data, calendar_history):
    st.subheader(f"Calendário {calendar_data['display_name']}")
    
    current_time_str = datetime.now().strftime("%H:%M")
    if not calendar_history['ts'] or calendar_history['ts'][-1] != current_time_str:
        calendar_history['ts'].append(current_time_str)
        calendar_history['z'].append(live_data['z_percent'])
    
    if calendar_data.get('alert_target', 0) > 0:
        if live_data['z_percent'] >= calendar_data['alert_target'] and not calendar_data.get('alert_sent', False):
            cal_type = calendar_data['type'].upper()
            msg = f"🎯 *ALERTA DE LUCRO ({cal_type})* 🎯\n\n*Ativo:* `{ticker}`\n*Calendário:* {cal_type} Strike {calendar_data['strike']:.2f}\n*Lucro Atual:* `{live_data['z_percent']:.2f}%`\n*Meta:* `{calendar_data['alert_target']:.2f}%`"
            send_telegram_message(msg)
            calendar_data['alert_sent'] = True
        elif live_data['z_percent'] < calendar_data['alert_target'] and calendar_data.get('alert_sent', False):
            calendar_data['alert_sent'] = False
            
    col1, col2 = st.columns(2)
    cal_type_upper = calendar_data['type'].upper()
    col1.metric(f"{cal_type_upper}F Now", f"{live_data['now_price_front']:.2f}", f"↑ TD: {calendar_data['td_price_front']:.2f}")
    col2.metric(f"{cal_type_upper}B Now", f"{live_data['now_price_back']:.2f}", f"↑ TD: {calendar_data['td_price_back']:.2f}")
    
    st.metric(f"%Z (Alvo: {calendar_data.get('alert_target', 0)}%)", f"{live_data['z_percent']:.2f}%")
    
    if len(calendar_history['z']) > 1:
        chart_data = pd.DataFrame({f"%Z {calendar_data['display_name']}": calendar_history['z']}, index=calendar_history['ts'])
        st.line_chart(chart_data)

    st.divider()

# ==============================================================================
# CORPO PRINCIPAL DO APP
# ==============================================================================
st.title("🗓️ Monitoramento de Calendários Duplos Pré-Earnings")

if 'positions' not in st.session_state:
    st.session_state.positions = load_positions_from_db()

with st.sidebar:
    # --- INÍCIO DO BLOCO DE CÓDIGO TEMPORÁRIO ---
    st.header("Admin (Temporário)")
    st.warning("Use este botão uma vez para limpar o histórico dos gráficos.")
    if st.button("Limpar Histórico dos Gráficos"):
        for ticker, pos_data in st.session_state.positions.items():
            # Limpa histórico dos calendários originais
            pos_data['history']['put_original']['ts'].clear()
            pos_data['history']['put_original']['z'].clear()
            pos_data['history']['call_original']['ts'].clear()
            pos_data['history']['call_original']['z'].clear()
            # Limpa histórico da volatilidade
            pos_data['history']['back_vol']['ts'].clear()
            pos_data['history']['back_vol']['vol'].clear()
            # Limpa histórico de todos os ajustes
            for i in range(len(pos_data.get('adjustments', []))):
                adj_key = f"adj_{i}"
                if adj_key in pos_data['history']:
                    pos_data['history'][adj_key]['ts'].clear()
                    pos_data['history'][adj_key]['z'].clear()
            
            # Salva os dados limpos de volta no DB
            update_position_in_db(ticker, pos_data)
        
        st.success("Histórico de todos os gráficos foi limpo!")
        time.sleep(2)
        st.rerun()
    st.divider()
    # --- FIM DO BLOCO DE CÓDIGO TEMPORÁRIO ---

    st.header("Adicionar Nova Posição")
    with st.form(key="add_position_form", clear_on_submit=True):
        ticker = st.text_input("Ticker do Ativo (ex: PETR4)").upper()
        put_strike = st.number_input("Strike da PUT", format="%.2f", step=0.01, key="p_s")
        td_price_pf = st.number_input("Preço TD - Put Front (Venda)", format="%.2f", step=0.01, key="p_pf")
        td_price_pb = st.number_input("Preço TD - Put Back (Compra)", format="%.2f", step=0.01, key="p_pb")
        put_alert_target = st.number_input("Alerta de Lucro % (PUT)", min_value=0.0, step=1.0, key="p_alert")
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
            front_exp_str = front_exp.strftime("%Y-%m-%d")
            back_exp_str = back_exp.strftime("%Y-%m-%d")
            fad_date = front_exp - timedelta(days=14)
            new_pos_data = {
                "put_original": {"type": "put", "display_name": "PUT Original", "strike": put_strike, "td_price_front": td_price_pf, "td_price_back": td_price_pb, "alert_target": put_alert_target, "alert_sent": False, "expirations": {"front": front_exp_str, "back": back_exp_str}},
                "call_original": {"type": "call", "display_name": "CALL Original", "strike": call_strike, "td_price_front": td_price_cf, "td_price_back": td_price_cb, "alert_target": call_alert_target, "alert_sent": False, "expirations": {"front": front_exp_str, "back": back_exp_str}},
                "fad_date": fad_date.strftime("%Y-%m-%d"), "td_back_vol": td_back_vol,
                "history": {"put_original": {"ts": [], "z": []}, "call_original": {"ts": [], "z": []}, "back_vol": {"ts": [], "vol": []}}, 
                "adjustments": []
            }
            add_position_to_db(ticker, new_pos_data)
            st.session_state.positions = load_positions_from_db()
            st.success(f"Posição em {ticker} adicionada ao banco de dados!")
            st.rerun()

if not st.session_state.positions:
    st.info("Nenhuma posição monitorada. Adicione uma na barra lateral.")
else:
    for ticker, data in list(st.session_state.positions.items()):
        with st.expander(f"Ativo: {ticker}", expanded=True):
            all_calendars = [data['put_original'], data['call_original']] + data.get('adjustments', [])
            live_data_list = []
            for cal_data in all_calendars:
                front_symbol = generate_option_symbol(ticker, cal_data['expirations']['front'], cal_data['strike'], cal_data['type'])
                back_symbol = generate_option_symbol(ticker, cal_data['expirations']['back'], cal_data['strike'], cal_data['type'])
                success_front, front_api_data = get_option_data(front_symbol)
                success_back, back_api_data = get_option_data(back_symbol)
                now_price_front = front_api_data['last'][0] if success_front and front_api_data.get('last') else 0
                now_price_back = back_api_data['last'][0] if success_back and back_api_data.get('last') else 0
                pl_info = calculate_pl_values(cal_data['td_price_back'], cal_data['td_price_front'], now_price_back, now_price_front)
                live_data_list.append({"now_price_front": now_price_front, "now_price_back": now_price_back, "back_api_data": back_api_data if success_back else None, **pl_info})
            
            col1, col2 = st.columns(2)
            with col1:
                render_calendar_block(ticker, data['put_original'], live_data_list[0], data['history']['put_original'])
            with col2:
                render_calendar_block(ticker, data['call_original'], live_data_list[1], data['history']['call_original'])
            
            for i, adj_data in enumerate(data.get('adjustments', [])):
                adj_history_key = f"adj_{i}"
                if adj_history_key not in data['history']: data['history'][adj_history_key] = {"ts": [], "z": []}
                if i % 2 == 0:
                    col1, col2 = st.columns(2)
                    with col1:
                        render_calendar_block(ticker, adj_data, live_data_list[i+2], data['history'][adj_history_key])
                else:
                    with col2:
                        render_calendar_block(ticker, adj_data, live_data_list[i+2], data['history'][adj_history_key])
            
            total_absolute_pl = sum(item['absolute_pl'] for item in live_data_list)
            total_initial_cost = sum(abs(item['initial_cost']) for item in live_data_list)
            total_pl_percent = (total_absolute_pl / total_initial_cost) * 100 if total_initial_cost != 0 else 0
            
            st.subheader("Resultado Consolidado")
            st.metric("P/L% Total do Trade", f"{total_pl_percent:.2f}%", f"R$ {total_absolute_pl:.2f}")
            st.divider()
            
            st.subheader("Controle Geral da Posição")
            back_data_p = live_data_list[0]['back_api_data']
            back_data_c = live_data_list[1]['back_api_data']
            back_vol_now_p = back_data_p['iv'][0] * 100 if back_data_p and back_data_p.get('iv') else 0
            back_vol_now_c = back_data_c['iv'][0] * 100 if back_data_c and back_data_c.get('iv') else 0
            back_vol_now = ((back_vol_now_p + back_vol_now_c) / 2) if back_vol_now_p or back_vol_now_c else 0
            
            current_time_str = datetime.now().strftime("%H:%M")
            vol_history = data['history']['back_vol']
            if not vol_history['ts'] or vol_history['ts'][-1] != current_time_str:
                vol_history['ts'].append(current_time_str)
                vol_history['vol'].append(back_vol_now)
            
            td_vol = data.get("td_back_vol", 0)
            st.metric("Vol Média Atual (Back)", f"{back_vol_now:.2f}%", f"↑ TD: {td_vol:.2f}%")
            if len(vol_history['vol']) > 1:
                chart_data_v = pd.DataFrame({'Back Vol': vol_history['vol']}, index=vol_history['ts'])
                st.line_chart(chart_data_v)
            
            fad_dt = datetime.strptime(data['fad_date'], "%Y-%m-%d").date()
            dias_para_fad = (fad_dt - datetime.now().date()).days
            st.info(f"**FAD (Final Adjustment Date):** {fad_dt.strftime('%d/%m/%Y')} (Faltam {dias_para_fad} dias)")
            
            if st.button("➕ Adicionar Ajuste", key=f"add_adj_{ticker}"): st.session_state.adjusting_ticker = ticker; st.rerun()
            if st.button("❌ Excluir Posição", key=f"del_{ticker}"):
                delete_position_from_db(ticker)
                st.session_state.positions = load_positions_from_db()
                if 'adjusting_ticker' in st.session_state: del st.session_state.adjusting_ticker
                st.rerun()

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
            position_to_update = st.session_state.positions[ticker_to_adjust]
            num_adjustments = len(position_to_update.get('adjustments', []))
            adj_exp_str = {"front": adj_front_exp.strftime("%Y-%m-%d"), "back": adj_back_exp.strftime("%Y-%m-%d")}
            new_adj = {
                "type": adj_type, "display_name": f"{adj_type.upper()} Ajuste {num_adjustments + 1}", "strike": adj_strike,
                "td_price_front": adj_td_price_front, "td_price_back": adj_td_price_back, "alert_target": adj_alert_target,
                "alert_sent": False, "expirations": adj_exp_str
            }
            position_to_update.setdefault('adjustments', []).append(new_adj)
            update_position_in_db(ticker_to_adjust, position_to_update)
            st.session_state.positions = load_positions_from_db()
            del st.session_state.adjusting_ticker
            st.rerun()

    if st.button("Cancelar Ajuste"): del st.session_state.adjusting_ticker; st.rerun()

for ticker, data in st.session_state.positions.items():
    update_position_in_db(ticker, data)

st.caption(f"Última atualização: {datetime.now().strftime('%H:%M:%S')}")
time.sleep(REFRESH_INTERVAL_SECONDS)
st.rerun()
