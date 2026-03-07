import json
import os
import numpy as np
from flask import Flask, request, render_template_string
from web3 import Web3
import plotly.graph_objects as go
import plotly.colors as pc

app = Flask(__name__)

# 配置 RPC 节点
PROVIDER_URL = os.environ.get("GETH_API", "http://10.219.60.235:8545")
w3 = Web3(Web3.HTTPProvider(PROVIDER_URL))

def get_plot_html(block_id):
    try:
        block = w3.eth.get_block(block_id, full_transactions=True)
    except Exception as e:
        return f"<div style='padding:20px;color:#ff4d4f;background:#fff;'>❌ Error: {str(e)}</div>"
    
    miner_raw = block.get('miner', '0x000...')
    # Miner 保留 0x + 8位
    miner_display = f"{miner_raw[:10]}..." if len(miner_raw) > 10 else miner_raw
    txs = block['transactions']
    
    if not txs:
        return f"<div style='padding:20px;color:#8c8c8c;background:#fff;'>Empty Block #{block_id}</div>"

    cols = 10 
    x_coords, y_coords, gas_values, hashes = [], [], [], []
    hover_texts = []

    for i, tx in enumerate(txs):
        gas_val = tx.get('gas', 0)
        gas_values.append(gas_val)
        
        x_coords.append(i % cols)
        y_coords.append(i // cols)
        
        gp_wei = tx.get('effectiveGasPrice') or tx.get('gasPrice') or 0
        gp_gwei = float(w3.from_wei(gp_wei, 'gwei'))
        tx_hash = f"0x{tx['hash'].hex()}"
        
        from_addr = str(tx.get('from') or "Unknown")
        to_addr_raw = tx.get('to')
        to_hover = f"{str(to_addr_raw)[:10]}..." if to_addr_raw else "Contract Creation"

        # 悬停文本：去掉数字逗号 (:.2f)，Miner保留8位
        hover_texts.append(
            f"📊 <b>Transaction #{i+1}</b><br>────────────────────<br>"
            f"🆔 Hash: {tx_hash[:14]}...<br>"
            f"⛽ Gas: {gas_val}<br>"
            f"💰 Price: {gp_gwei:.2f} Gwei<br>"
            f"📤 From: {from_addr[:10]}...<br>"
            f"📥 To: {to_hover}"
        )

    # 计算颜色深度（对 Gas 取 Log 方便视觉区分）
    log_gas = [np.log10(v) if v > 0 else 0 for v in gas_values]
    v_min, v_max = min(log_gas), max(log_gas)
    if v_min == v_max: v_min -= 0.1; v_max += 0.1

    row_count = (len(txs) + cols - 1) // cols
    plotly_height = row_count * 52 + 20

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=x_coords, y=y_coords, 
        mode='markers', # 去掉 text，方块内不写数字
        marker=dict(
            symbol='square', size=45, 
            color=log_gas, 
            colorscale='YlOrRd', # 黄到红
            showscale=True, 
            line=dict(width=1, color='#eee'),
            colorbar=dict(
                title="Gas (Log)", 
                thickness=15, 
                tickformat=".2f" # 侧边栏去掉逗号
            )
        ),
        hovertext=hover_texts, 
        hoverinfo='text'
    ))

    fig.update_layout(
        width=580,
        height=plotly_height,
        xaxis=dict(visible=False, fixedrange=True, range=[-0.6, cols - 0.4]),
        yaxis=dict(
            visible=False, 
            fixedrange=True, 
            autorange='reversed', 
            scaleanchor="x"
        ),
        margin=dict(l=10, r=10, t=10, b=10),
        paper_bgcolor='white', # 背景白色
        plot_bgcolor='white'
    )

    plot_div = fig.to_html(include_plotlyjs=False, full_html=False, config={'displayModeBar': False})
    
    html_header = f"""
    <div style="padding: 15px 20px 10px 20px; border-bottom: 1px solid #eee; background: #fff;">
        <div style="font-size: 18px; font-weight: bold; color: #222;">Block #{block_id}</div>
        <div style="font-size: 11px; color: #666; font-family: monospace; margin-top: 4px;">
            Miner: <span style="color: #444;">{miner_display}</span>
        </div>
    </div>
    """

    return f"""
    <div class="mosaic-card" style="background: white; border-radius: 12px; box-shadow: 0 4px 20px rgba(0,0,0,0.08); border: 1px solid #eee; margin-bottom: 30px; overflow: hidden;">
        {html_header}
        <div class="mosaic-plot-area" style="padding: 5px;">
            {plot_div}
        </div>
    </div>
    <script>
    (function() {{
        var plotDivs = document.querySelectorAll('.plotly-graph-div');
        var currentDiv = plotDivs[plotDivs.length - 1];
        if(currentDiv) {{
            currentDiv.on('plotly_click', function(data){{
                var hashList = {json.dumps(hashes)};
                var idx = data.points[0].pointIndex;
                var targetHash = hashList[idx];
                navigator.clipboard.writeText(targetHash).then(function() {{
                    alert('📋 Transaction Hash Copied:\\n' + targetHash);
                }});
            }});
        }}
    }})();
    </script>
    """

@app.route('/')
def home():
    return render_template_string("""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Transaction Mosaic Clean</title>
        <script src="https://cdn.plot.ly/plotly-2.24.1.min.js"></script>
        <style>
            body { font-family: -apple-system, sans-serif; background: #fff; display: flex; flex-direction: column; align-items: center; padding: 20px; }
            .controls { background: #f9f9f9; padding: 15px 25px; border-radius: 50px; margin-bottom: 25px; display: flex; gap: 10px; border: 1px solid #eee; }
            input { padding: 8px 15px; border: 1px solid #ddd; border-radius: 20px; outline: none; }
            button { padding: 8px 20px; background: #000; color: #fff; border: none; border-radius: 20px; cursor: pointer; }
            #container { width: 600px; }
        </style>
    </head>
    <body>
        <div class="controls">
            <input type="number" id="blockId" placeholder="Enter Block Number">
            <button onclick="loadBlock()">Visualize Transactions</button>
        </div>
        <div id="container"></div>
        <script>
        async function loadBlock() {
            const bid = document.getElementById('blockId').value;
            if(!bid) return;
            const res = await fetch('/api/explore?block_id=' + bid);
            const html = await res.text();
            const div = document.createElement('div');
            div.innerHTML = html;
            document.getElementById('container').prepend(div);
            div.querySelectorAll("script").forEach(s => eval(s.text));
        }
        </script>
    </body>
    </html>
    """)

@app.route('/api/explore')
def explore():
    bid = request.args.get('block_id', type=int)
    return get_plot_html(bid) if bid else ("Error", 400)

if __name__ == '__main__':
    app.run(debug=True, port=5000)