from web3 import Web3
from web3.providers.rpc import HTTPProvider
from web3.middleware import ExtraDataToPOAMiddleware #Necessary for POA chains
from datetime import datetime
import json
import os
import pandas as pd


def connect_to(chain):
    if chain == 'source':  # The source contract chain is avax
        api_url = f"https://api.avax-test.network/ext/bc/C/rpc" #AVAX C-chain testnet

    if chain == 'destination':  # The destination contract chain is bsc
        api_url = f"https://data-seed-prebsc-1-s1.binance.org:8545/" #BSC testnet

    if chain in ['source','destination']:
        w3 = Web3(Web3.HTTPProvider(api_url))
        # inject the poa compatibility middleware to the innermost layer
        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    return w3


def get_contract_info(chain, contract_info):
    """
        Load the contract_info file into a dictionary
        This function is used by the autograder and will likely be useful to you
    """
    try:
        with open(contract_info, 'r')  as f:
            contracts = json.load(f)
    except Exception as e:
        print( f"Failed to read contract info\nPlease contact your instructor\n{e}" )
        return 0
    return contracts[chain]

def event_id(event):
    return f"{event['transactionHash'].hex()}_{event['logIndex']}"


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            state = json.load(f)
    else:
        state = {}

    state.setdefault("processed", {})
    state["processed"].setdefault("source", [])
    state["processed"].setdefault("destination", [])

    state.setdefault("last_scanned", {})
    state["last_scanned"].setdefault("source", 0)
    state["last_scanned"].setdefault("destination", 0)

    return state


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def get_logs_chunked(event_builder, from_block, to_block, chunk_size=CHUNK_SIZE):
    logs = []
    start = from_block

    while start <= to_block:
        end = min(start + chunk_size - 1, to_block)
        chunk_logs = event_builder.get_logs(from_block=start, to_block=end)
        logs.extend(chunk_logs)
        start = end + 1

    return logs


def build_and_send_tx(w3, account, tx_func, nonce):
    tx = tx_func.build_transaction({
        "from": account.address,
        "nonce": nonce,
        "chainId": w3.eth.chain_id,
        "gasPrice": w3.eth.gas_price,
    })

    try:
        tx["gas"] = w3.eth.estimate_gas(tx)
    except Exception:
        tx["gas"] = 500000

    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)

    if receipt.status != 1:
        raise RuntimeError(f"Transaction failed: {tx_hash.hex()}")

    return tx_hash.hex()


def scan_blocks(chain, contract_info="contract_info.json"):
    if chain not in ["source", "destination"]:
        print(f"Invalid chain: {chain}")
        return 0

    with open(contract_info, "r") as f:
        full = json.load(f)

    private_key = full["private_key"]
    acct = Web3().eth.account.from_key(private_key)
    state = load_state()

    if chain == "source":
        w3_source = connect_to("source")
        w3_dest = connect_to("destination")

        source_contract = w3_source.eth.contract(
            address=Web3.to_checksum_address(full["source"]["address"]),
            abi=full["source"]["abi"]
        )
        dest_contract = w3_dest.eth.contract(
            address=Web3.to_checksum_address(full["destination"]["address"]),
            abi=full["destination"]["abi"]
        )

        latest = w3_source.eth.block_number
        from_block = state["last_scanned"]["source"] + 1

        if from_block > latest:
            return 1

        try:
            events = get_logs_chunked(
                source_contract.events.Deposit(),
                from_block,
                latest,
                CHUNK_SIZE
            )
        except Exception as err:
            print(f"Error scanning source logs: {err}")
            return 0

        events = sorted(events, key=lambda e: (e["blockNumber"], e["logIndex"]))
        processed_set = set(state["processed"]["source"])
        nonce = w3_dest.eth.get_transaction_count(acct.address)

        for e in events:
            eid = event_id(e)
            if eid in processed_set:
                continue

            args = e["args"]

            try:
                tx_hash = build_and_send_tx(
                    w3_dest,
                    acct,
                    dest_contract.functions.wrap(
                        args["token"],
                        args["recipient"],
                        args["amount"]
                    ),
                    nonce
                )
                print(f"Wrapped deposit event {eid} in tx {tx_hash}")
                state["processed"]["source"].append(eid)
                processed_set.add(eid)
                nonce += 1
            except Exception as err:
                print(f"Failed to relay Deposit event {eid}: {err}")

        state["last_scanned"]["source"] = latest
        save_state(state)
        return 1

    else:  # destination
        w3_dest = connect_to("destination")
        w3_source = connect_to("source")

        dest_contract = w3_dest.eth.contract(
            address=Web3.to_checksum_address(full["destination"]["address"]),
            abi=full["destination"]["abi"]
        )
        source_contract = w3_source.eth.contract(
            address=Web3.to_checksum_address(full["source"]["address"]),
            abi=full["source"]["abi"]
        )

        latest = w3_dest.eth.block_number
        from_block = state["last_scanned"]["destination"] + 1

        if from_block > latest:
            return 1

        try:
            events = get_logs_chunked(
                dest_contract.events.Unwrap(),
                from_block,
                latest,
                CHUNK_SIZE
            )
        except Exception as err:
            print(f"Error scanning destination logs: {err}")
            return 0

        events = sorted(events, key=lambda e: (e["blockNumber"], e["logIndex"]))
        processed_set = set(state["processed"]["destination"])
        nonce = w3_source.eth.get_transaction_count(acct.address)

        for e in events:
            eid = event_id(e)
            if eid in processed_set:
                continue

            args = e["args"]

            try:
                tx_hash = build_and_send_tx(
                    w3_source,
                    acct,
                    source_contract.functions.withdraw(
                        args["underlying_token"],
                        args["to"],
                        args["amount"]
                    ),
                    nonce
                )
                print(f"Withdrew for unwrap event {eid} in tx {tx_hash}")
                state["processed"]["destination"].append(eid)
                processed_set.add(eid)
                nonce += 1
            except Exception as err:
                print(f"Failed to relay Unwrap event {eid}: {err}")

        state["last_scanned"]["destination"] = latest
        save_state(state)
        return 1
