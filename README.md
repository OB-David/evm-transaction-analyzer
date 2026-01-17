# Control Flow Graph for EVM



## Using UV
You need to install [UV](https://docs.astral.sh/uv/) at first

### How to use UV for python version management
- `uv python list`: View available Python versions.
- `uv python install python3.x`: Install Python versions.
- `uv python uninstall python3.x`: Uninstall a Python version.

### How to manage Python projects with UV
- `uv init`: Create a new Python project with a `pyproject.toml` file, which contains all information for this project, similar to `package.json` in Node.js project.
- `uv add`: Add a dependency to the project, instead of `pip install`, `uv add` will add the package to `pyproject.toml` environment.
- `uv remove`: Remove a dependency from the project.
- `uv sync`: Sync the project's dependencies with the environment, similar to `npm install`.
- `uv lock`: Create a lockfile for the project's dependencies.
- `uv run`: Run a command in the project environment.
- `uv tree`: View the dependency tree for the project.

## What each file does

`main.py`, `evm_information.py`, `basic_block.py`, `cfg_transaction.py`, `cfg_contract.py`, and `cfg_static_complete.py` are the main files for this project.

- `evm_information.py` retrieves and standardizes execution traces and contract bytecode from an Ethereum node, serving as a clean data interface for downstream analysis.

- `basic_block.py` splits EVM bytecode into basic blocks, enabling structural analysis of smart contract execution.

- `cfg_transaction.py` draws the transaction execution CFG of a certain transaction.

- `cfg_contract.py` draws the contract CFG of the executed path of a certain contract.

- `cfg_static_complete.py` draws the static CFG of a certain contract.

> The results of the 3 CFGs above are saved in the folder `Result/`.

Run the following command to draw the CFGs:

```bash
python main.py
```

### Tool files for detecting Swap patterns

- `find_call_nodes.py` extracts the nodes containing `"CALL"` and `"SSTORE"` from the (dynamic) contract CFG.  
  The results are saved in the folder `Result_call_nodes/`.

  Run the following command to extract the nodes:

```bash
python find_call_nodes.py
```

- `find_trace_opcode.py` extracts the steps with opcodes `"CALL"` or `"SSTORE"` from the execution trace.  
  It first targets a certain contract in a transaction, and then extracts the relevant steps.  
  The results are saved in the folder `Result_call_sstore/`.

Run the following command to extract the steps:

```bash
python find_trace_opcode.py
```