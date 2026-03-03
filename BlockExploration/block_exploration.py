import json
import os
import numpy as np
from flask import Flask, request, render_template_string
from web3 import Web3
import plotly.graph_objects as go
import plotly.colors as pc

app = Flask(__name__)

# 配置 RPC 节点
PROVIDER_URL = os.environ.get("GETH_API")
w3 = Web3(Web3.HTTPProvider(PROVIDER_URL))

def get_plot_html(block_id):
    try:
        block = w3.eth.get_block(block_id, full_transactions=True)
    except Exception as e:
        return f"<div style='padding:20px;color:#ff4d4f;background:#fff;'>❌ Error: {str(e)}</div>"
    
    miner = block.get('miner', '0x000...')
    txs = block['transactions']
    
    if not txs:
        return f"<div style='padding:20px;color:#8c8c8c;background:#fff;'>Empty Block #{block_id}</div>"

    cols = 10 
    x_coords, y_coords, log_gas, hashes = [], [], [], []
    hover_texts, tx_labels = [], []

    for i, tx in enumerate(txs):
        gas_val = tx.get('gas', 0)
        log_v = np.log10(gas_val) if gas_val > 0 else 0
        log_gas.append(log_v)
        x_coords.append(i % cols)
        y_coords.append(i // cols)
        
        gp_wei = tx.get('effectiveGasPrice') or tx.get('gasPrice') or 0
        gp_gwei = float(w3.from_wei(gp_wei, 'gwei'))
        tx_hash = f"0x{tx['hash'].hex()}"
        
        from_addr = str(tx.get('from') or "Unknown")
        to_addr_raw = tx.get('to')
        to_hover = f"{str(to_addr_raw)[:10]}..." if to_addr_raw else "Contract Creation"

        tx_labels.append(f"Tx_{i+1}") 
        hashes.append(tx_hash)
        hover_texts.append(
            f"📊 Transaction #{i+1}<br>────────────────────<br>"
            f"🆔 Hash: {tx_hash[:14]}...<br>⛽ Gas: {gas_val}<br>"
            f"💰 Price: {gp_gwei:.2f} Gwei<br>📤 From: {from_addr[:10]}...<br>📥 To: {to_hover}"
        )

    v_min, v_max = min(log_gas), max(log_gas)
    if v_min == v_max: v_min -= 0.1; v_max += 0.1
    colorscale = pc.get_colorscale('YlOrRd')
    hover_bg_colors = [pc.sample_colorscale(colorscale, [(v - v_min) / (v_max - v_min)])[0] for v in log_gas]

    row_count = (len(txs) + cols - 1) // cols
    
    # 既然手动生成标题，图表顶部边距可以设得很小
    top_margin_px = 5
    plotly_height = row_count * 52 + top_margin_px

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=x_coords, y=y_coords, mode='markers+text',
        marker=dict(
            symbol='square', size=48, color=log_gas, colorscale='YlOrRd',
            showscale=True, line=dict(width=2, color='white'),
            colorbar=dict(
                title="Gas", thickness=15, len=0.7,
                tickvals=[v_min, (v_min+v_max)/2, v_max],
                ticktext=[str(int(10**v)) for v in [v_min, (v_min+v_max)/2, v_max]]
            )
        ),
        text=tx_labels, textfont=dict(size=8, color="black"),
        hovertext=hover_texts, hoverinfo='text',
        hoverlabel=dict(bgcolor=hover_bg_colors, font_color="#000000", font_family="monospace")
    ))

    fig.update_layout(
        # --- 彻底去掉 Plotly 内部标题 ---
        width=580,
        height=plotly_height,
        xaxis=dict(visible=False, fixedrange=True, range=[-0.6, cols - 0.4]),
        yaxis=dict(
            visible=False, 
            fixedrange=True, 
            autorange='reversed', 
            scaleanchor="x",
            domain=[0, 1] 
        ),
        margin=dict(l=10, r=10, t=top_margin_px, b=10, pad=0),
        autosize=False,
        plot_bgcolor='rgba(0,0,0,0)',
        paper_bgcolor='rgba(0,0,0,0)'
    )

    plot_div = fig.to_html(include_plotlyjs=False, full_html=False, config={'displayModeBar': False})
    
    # --- 手动生成 HTML 标题部分 ---
    html_header = f"""
    <div style="padding: 15px 20px 10px 20px; border-bottom: 1px solid #eee;">
        <div style="font-size: 18px; font-weight: bold; color: #222;">Block #{block_id}</div>
        <div style="font-size: 11px; color: #666; font-family: monospace; margin-top: 4px;">
            Miner: <span style="color: #444;">{miner}</span>
        </div>
    </div>
    """

    container_style = (
        "max-height: 850px; "
        "overflow-y: auto; "
        "overflow-x: hidden; "
        "background: white; "
        "border-radius: 12px; "
        "box-shadow: 0 4px 20px rgba(0,0,0,0.08); "
        "border: 1px solid #eee; "
        "margin-bottom: 30px;"
    )

    return f"""
    <div class="mosaic-card" style="{container_style}">
        {html_header}
        <div class="mosaic-plot-area" style="padding-top: 5px;">
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
                navigator.clipboard.writeText(hashList[idx]).then(function() {{
                    alert('📋 Copied Hash:\\n' + hashList[idx]);
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
        <title>Gas Mosaic Pro</title>
        <script src="https://cdn.plot.ly/plotly-2.24.1.min.js"></script>
        <style>
            body { font-family: -apple-system, system-ui, sans-serif; background: #f8f9fa; display: flex; flex-direction: column; align-items: center; padding: 20px; }
            .controls { background: white; padding: 15px 25px; border-radius: 50px; margin-bottom: 25px; display: flex; gap: 10px; box-shadow: 0 2px 15px rgba(0,0,0,0.05); }
            input { padding: 8px 15px; border: 1px solid #eee; border-radius: 20px; outline: none; width: 180px; }
            button { padding: 8px 20px; background: #222; color: white; border: none; border-radius: 20px; cursor: pointer; font-weight: 500; }
            button:hover { background: #444; }
            #container { width: 600px; }
            .mosaic-card::-webkit-scrollbar { width: 6px; }
            .mosaic-card::-webkit-scrollbar-thumb { background: #ddd; border-radius: 10px; }
        </style>
    </head>
    <body>
        <div class="controls">
            <input type="number" id="blockId" placeholder="Block Number">
            <button onclick="loadBlock()">Visualize</button>
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