# Transaction's Control Flow Graph for EVM



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

- `evm_information.py` retrieves and standardizes execution traces and contract bytecode from an Ethereum node, serving as a clean data interface for downstream analyse. Now it also calculate the contracts', users' addresses and map between slots and addresses. 

- `basic_block.py` splits EVM bytecode into basic blocks, enabling structural analysis of smart contract execution.

- `cfg_transaction.py` draws the transaction execution CFG of a certain transaction.

> The results of the 3 CFGs above are saved in the folder `Result/`.

Run the following command to draw the CFGs:

```bash
python main.py
```

