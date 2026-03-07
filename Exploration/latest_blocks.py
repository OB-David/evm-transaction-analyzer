import time
import threading
from flask import Flask, request, jsonify
from flask_cors import CORS
from web3 import Web3
import plotly.graph_objects as go

app = Flask(__name__)
CORS(app)

# --- 配置区 ---
PROVIDER_URL = 'http://10.219.60.235:8545'
w3 = Web3(Web3.HTTPProvider(PROVIDER_URL))

# --- 内存缓存池 ---
BLOCK_CACHE = {}
LATEST_BLOCK_INFO = {"num": 0, "time": 0}
PREFETCH_COUNT = 300

# --- 核心逻辑：预取函数 ---
def prefetch_worker(start_num):
    """后台预取：从指定高度向后计算 PREFETCH_COUNT 个块"""
    print(f"[*] Background prefetch started: Target {PREFETCH_COUNT} blocks.")
    
    for i in range(PREFETCH_COUNT):
        target_num = start_num - i
        if target_num < 0: break
        
        s_num = str(target_num)
        if s_num not in BLOCK_CACHE:
            try:
                # 仅获取基础信息以提高速度
                block = w3.eth.get_block(target_num, False)
                tx_count = len(block.get('transactions', []))
                gas_used = block.get('gasUsed', 0)
                avg_gas = gas_used / tx_count if tx_count > 0 else 0
                base_fee = block.get('baseFeePerGas', 0) / 1e9
                
                BLOCK_CACHE[s_num] = {
                    "bnum": s_num,
                    "avg_gas": avg_gas,
                    "base_fee": base_fee,
                    "miner": block.get('miner', '0x000...000')
                }
                if (i + 1) % 50 == 0:
                    print(f"  [+] Cached {i + 1} blocks...")
            except Exception as e:
                continue 
    print("[*] Background prefetch completed.")

def get_latest_num():
    """获取最新高度并更新缓存时间"""
    now = time.time()
    if now - LATEST_BLOCK_INFO["time"] > 5:
        try:
            LATEST_BLOCK_INFO["num"] = w3.eth.block_number
            LATEST_BLOCK_INFO["time"] = now
        except Exception as e:
            print(f"Node connection error: {e}")
    return LATEST_BLOCK_INFO["num"]

def get_blocks_data(offset=0, count=100):
    latest_num = get_latest_num()
    start_num = latest_num - offset
    
    # 获取当前页面最顶端块的时间戳
    try:
        page_latest_block = w3.eth.get_block(start_num, False)
        page_timestamp = page_latest_block.get('timestamp', 0)
    except:
        page_timestamp = 0
    
    block_list = []
    # 构造目标块号列表（升序排列）
    target_nums = [start_num - i for i in range(count - 1, -1, -1) if start_num - i >= 0]
    
    for num in target_nums:
        s_num = str(num)
        if s_num in BLOCK_CACHE:
            block_list.append(BLOCK_CACHE[s_num])
        else:
            # 缓存未命中（例如预取还没跑完），实时获取
            block = w3.eth.get_block(num, False)
            tx_count = len(block.get('transactions', []))
            gas_used = block.get('gasUsed', 0)
            avg_gas = gas_used / tx_count if tx_count > 0 else 0
            base_fee = block.get('baseFeePerGas', 0) / 1e9
            data = {"bnum": s_num, "avg_gas": avg_gas, "base_fee": base_fee}
            BLOCK_CACHE[s_num] = data
            block_list.append(data)
            
    return block_list, page_timestamp

def create_plot_json(block_data):
    if not block_data: return None
    cols = 10
    avg_gases = [d['avg_gas'] for d in block_data]
    x_coords = [i % cols for i in range(len(block_data))]
    y_coords = [i // cols for i in range(len(block_data))]
    
    hover_texts = [f"📦 <b>Block: {d['bnum']}</b><br>⛽ Avg Gas: {d['avg_gas']:.2f}" for d in block_data]

    fig = go.Figure(go.Scatter(
        x=x_coords, y=y_coords, mode='markers',
        marker=dict(
            symbol='square', size=42, color=avg_gases,
            colorscale='YlOrRd', showscale=True,
            colorbar=dict(
                title="Avg Gas", thickness=18, x=1.02, 
                len=0.8, y=0.5, yanchor='middle', 
                tickformat=".2f"
            ),
            line=dict(width=1, color='#eee')
        ),
        hovertext=hover_texts, hoverinfo='text'
    ))

    fig.update_layout(
        width=600, height=580,
        xaxis=dict(visible=False, fixedrange=True, range=[-0.8, 9.8]),
        yaxis=dict(visible=False, fixedrange=True, autorange='reversed', scaleanchor="x", scaleratio=1),
        paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)',
        margin=dict(l=30, r=80, t=20, b=20) 
    )
    return fig.to_json()

@app.route('/api/data')
def api_data():
    offset = request.args.get('offset', default=0, type=int)
    blocks, page_time = get_blocks_data(offset)
    plot_json = create_plot_json(blocks)
    return jsonify({"plot_json": plot_json, "page_time": page_time})

if __name__ == '__main__':
    # --- 关键：启动时立即执行一次预取 ---
    try:
        initial_height = w3.eth.block_number
        # 开启后台线程，不阻塞 Flask 启动
        threading.Thread(target=prefetch_worker, args=(initial_height,), daemon=True).start()
    except Exception as e:
        print(f"Failed to start prefetch thread: {e}")

    print("Backend API is running on http://127.0.0.1:5003")
    app.run(debug=True, port=5003)