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
        if e.response.status_code == 400:
            st.toast(f"Erro 400: Símbolo da opção inválido: {option_symbol}", icon="🚨")
        else:
            st.toast(f"Erro de API para {option_symbol}: {e}", icon="🚨")
        return None
    except requests.exceptions.RequestException as e:
        st.toast(f"Erro de conexão para {option_symbol}: {e}", icon="🚨")
        return None

# ALTERADO: Função ajustada para o padrão OCC de 8 dígitos no strike
def generate_option_symbol(ticker, exp_date, strike, option_type):
    """Gera o código da opção no formato da B3 (padrão OCC)."""
    exp_dt = datetime.strptime(exp_date, "%Y-%m-%d")
    # O padrão OCC usa 8 dígitos para o strike (valor * 1000)
    # Ex: strike 35.50 -> 35500 -> '00035500'
    strike_part = f"{int(strike * 1000):08d}"
    
    # Remove o número do final do ticker se for uma ação (ex: PETR4 -> PETR)
    # A API geralmente espera o ticker base para formar o símbolo da opção.
    base_ticker = ''.join([i for i in ticker if not i.isdigit()])
    
    return f"{base_ticker}{exp_dt.strftime('%y%m%d')}{option_type.upper()}{strike_part}"

def calculate_z_percent(td_price_back, td_price_front, now_price_back, now_price_front):
    initial_cost = td_price_back - td_price_front
    current_value = now_price_back - now_price_front
    if initial_cost == 0: return 0.0
    profit_loss = current_value - initial_cost
    return (profit_loss / abs(initial_cost)) * 100 if initial_cost != 0 else 0.0

# ==============================================================================
# CORPO PRINCIPAL DO APP
# ==============================================================================
st.set_page_config(page_title="Monitor de Calendários", layout="wide")
st.markdown("### 🗓️ Monitoramento de Calendários Duplos Pré-Earnings")

if 'positions' not in st.session_state:
    st.session_state.positions = load_positions()

# (O restante do código, incluindo o formulário da sidebar, permanece o mesmo)
with st.sidebar:
    st.header("Adicionar Nova Posição")
    with st.form(key="add_calendar_form", clear_on_submit=True):
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

        st.subheader("Vencimentos")
        front_exp = st.date_input("Vencimento Front (Curto)")
        back_exp = st.date_input("Vencimento Back (Longo)")
        
        submitted = st.form_submit_button("Adicionar Monitoramento")

        if submitted and ticker:
            if not all([put_strike > 0, call_strike > 0, td_price_pf > 0, td_price_pb > 0, td_price_cf > 0, td_price_cb > 0]):
                st.error("Todos os preços e strikes devem ser maiores que zero.")
            elif front_exp >= back_exp:
                st.error("A data de vencimento 'Front' deve ser anterior à 'Back'.")
            else:
                front_exp_str = front_exp.strftime("%Y-%m-%d")
                back_exp_str = back_exp.strftime("%Y-%m-%d")
                fad_date = front_exp - timedelta(days=14)
                
                new_pos = {
                    "put_calendar": {"strike": put_strike, "td_price_front": td_price_pf, "td_price_back": td_price_pb, "alert_target": put_alert_target, "alert_sent": False },
                    "call_calendar": {"strike": call_strike, "td_price_front": td_price_cf, "td_price_back": td_price_cb, "alert_target": call_alert_target, "alert_sent": False},
                    "expirations": {"front": front_exp_str, "back": back_exp_str}, "fad_date": fad_date.strftime("%Y-%m-%d"),
                    "history": {"timestamp": [], "put_z": [], "call_z": [], "back_vol": []}, "adjustments": []
                }
                
                st.session_state.positions[ticker] = new_pos
                save_positions(st.session_state.positions)
                st.success(f"Posição em {ticker} adicionada!")
                st.rerun()

if not st.session_state.positions:
    st.info("Nenhuma posição monitorada. Adicione uma na barra lateral.")
else:
    for ticker, data in list(st.session_state.positions.items()):
        with st.expander(f"Ativo: {ticker}", expanded=True):
            pf_symbol = generate_option_symbol(ticker, data['expirations']['front'], data['put_calendar']['strike'], 'p')
            pb_symbol = generate_option_symbol(ticker, data['expirations']['back'], data['put_calendar']['strike'], 'p')
            cf_symbol = generate_option_symbol(ticker, data['expirations']['front'], data['call_calendar']['strike'], 'c')
            cb_symbol = generate_option_symbol(ticker, data['expirations']['back'], data['call_calendar']['strike'], 'c')

            pf_data = get_option_data(pf_symbol)
            pb_data = get_option_data(pb_symbol)
            cf_data = get_option_data(cf_symbol)
            cb_data = get_option_data(cb_symbol)
            
            now_price_pf = pf_data['last'][0] if pf_data and pf_data.get('last') else 0
            now_price_pb = pb_data['last'][0] if pb_data and pb_data.get('last') else 0
            now_price_cf = cf_data['last'][0] if cf_data and cf_data.get('last') else 0
            now_price_cb = cb_data['last'][0] if cb_data and cb_data.get('last') else 0
            
            back_vol_now_p = pb_data['iv'][0] if pb_data and pb_data.get('iv') else 0
            back_vol_now_c = cb_data['iv'][0] if cb_data and cb_data.get('iv') else 0
            back_vol_now = ((back_vol_now_p + back_vol_now_c) / 2) * 100 if back_vol_now_p and back_vol_now_c else 0

            put_z = calculate_z_percent(data['put_calendar']['td_price_back'], data['put_calendar']['td_price_front'], now_price_pb, now_price_pf)
            call_z = calculate_z_percent(data['call_calendar']['td_price_back'], data['call_calendar']['td_price_front'], now_price_cb, now_price_cf)

            current_time = datetime.now()
            if not data['history']['timestamp'] or data['history']['timestamp'][-1] != current_time.strftime("%H:%M"):
                data['history']['timestamp'].append(current_time.strftime("%H:%M"))
                data['history']['put_z'].append(put_z)
                data['history']['call_z'].append(call_z)
                data['history']['back_vol'].append(back_vol_now)
            
            put_info = data['put_calendar']
            if put_info.get('alert_target', 0) > 0:
                if put_z >= put_info['alert_target'] and not put_info.get('alert_sent', False):
                    msg = (f"🎯 *ALERTA DE LUCRO (PUT)* 🎯\n\n*Ativo:* `{ticker}`\n*Calendário:* PUT Strike {put_info['strike']:.2f}\n*Lucro Atual:* `{put_z:.2f}%`\n*Meta:* `{put_info['alert_target']:.2f}%`")
                    send_telegram_message(msg)
                    st.session_state.positions[ticker]['put_calendar']['alert_sent'] = True
                elif put_z < put_info['alert_target'] and put_info.get('alert_sent', False):
                    st.session_state.positions[ticker]['put_calendar']['alert_sent'] = False
            
            call_info = data['call_calendar']
            if call_info.get('alert_target', 0) > 0:
                if call_z >= call_info['alert_target'] and not call_info.get('alert_sent', False):
                    msg = (f"🎯 *ALERTA DE LUCRO (CALL)* 🎯\n\n*Ativo:* `{ticker}`\n*Calendário:* CALL Strike {call_info['strike']:.2f}\n*Lucro Atual:* `{call_z:.2f}%`\n*Meta:* `{call_info['alert_target']:.2f}%`")
                    send_telegram_message(msg)
                    st.session_state.positions[ticker]['call_calendar']['alert_sent'] = True
                elif call_z < call_info['alert_target'] and call_info.get('alert_sent', False):
                    st.session_state.positions[ticker]['call_calendar']['alert_sent'] = False

            col_p, col_c, col_vol = st.columns(3)
            with col_p:
                st.subheader("P")
                c1, c2 = st.columns(2)
                c1.metric("PF Now", f"{now_price_pf:.2f}", f"↑ TD: {data['put_calendar']['td_price_front']:.2f}")
                c2.metric("PB Now", f"{now_price_pb:.2f}", f"↑ TD: {data['put_calendar']['td_price_back']:.2f}")
                st.metric(f"%Z Put (Alvo: {put_info['alert_target']}%)", f"{put_z:.2f}%")
                if len(data['history']['put_z']) > 1:
                    chart_data_p = pd.DataFrame({'%Z Put': data['history']['put_z']}, index=data['history']['timestamp'])
                    st.line_chart(chart_data_p)
            with col_c:
                st.subheader("C")
                c1, c2 = st.columns(2)
                c1.metric("CF Now", f"{now_price_cf:.2f}", f"↑ TD: {data['call_calendar']['td_price_front']:.2f}")
                c2.metric("CB Now", f"{now_price_cb:.2f}", f"↑ TD: {data['call_calendar']['td_price_back']:.2f}")
                st.metric(f"%Z Call (Alvo: {call_info['alert_target']}%)", f"{call_z:.2f}%")
                if len(data['history']['call_z']) > 1:
                    chart_data_c = pd.DataFrame({'%Z Call': data['history']['call_z']}, index=data['history']['timestamp'])
                    st.line_chart(chart_data_c)
            with col_vol:
                st.subheader("Back Vol")
                td_vol = data.get("td_back_vol", back_vol_now)
                if "td_back_vol" not in data: data["td_back_vol"] = td_vol
                st.metric("Vol Atual", f"{back_vol_now:.2f}%", f"↑ TD: {td_vol:.2f}%")
                if len(data['history']['back_vol']) > 1:
                    chart_data_v = pd.DataFrame({'Back Vol': data['history']['back_vol']}, index=data['history']['timestamp'])
                    st.line_chart(chart_data_v)
            st.divider()
            fad_dt = datetime.strptime(data['fad_date'], "%Y-%m-%d").date()
            dias_para_fad = (fad_dt - datetime.now().date()).days
            if dias_para_fad <= 7:
                st.warning(f"**FAD (Final Adjustment Date):** {fad_dt.strftime('%d/%m/%Y')} (Faltam {dias_para_fad} dias)")
            else:
                st.info(f"**FAD (Final Adjustment Date):** {fad_dt.strftime('%d/%m/%Y')} (Faltam {dias_para_fad} dias)")
            if st.button("❌ Excluir Posição", key=f"del_{ticker}"):
                del st.session_state.positions[ticker]
                save_positions(st.session_state.positions)
                st.rerun()
    
    save_positions(st.session_state.positions)

st.caption(f"Última atualização: {datetime.now().strftime('%H:%M:%S')}")
time.sleep(REFRESH_INTERVAL_SECONDS)
st.rerun()
