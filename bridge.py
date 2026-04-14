from web3 import Web3
from web3.providers.rpc import HTTPProvider
from web3.middleware import ExtraDataToPOAMiddleware  # Necessary for POA chains
from datetime import datetime
from pathlib import Path
import json
import pandas as pd


STATE_FILE = "bridge_state.json"


def connect_to(chain):
    if chain == 'source':  # The source contract chain is avax
        api_url = "https://api.avax-test.network/ext/bc/C/rpc"  # AVAX C-chain testnet
    elif chain == 'destination':  # The destination contract chain is bsc
        api_url = "https://data-seed-prebsc-1-s1.binance.org:8545/"  # BSC testnet
    else:
        raise ValueError(f"Invalid chain: {chain}")

    w3 = Web3(Web3.HTTPProvider(api_url))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    return w3


def get_contract_info(chain, contract_info):
    """
    Load the contract_info file into a dictionary.
    Returns contracts["source"] or contracts["destination"].
    """
    try:
        with open(contract_info, 'r') as f:
            contracts = json.load(f)
    except Exception as e:
        print(f"Failed to read contract info\nPlease contact your instructor\n{e}")
        return 0
    return contracts[chain]


def load_full_contract_info(contract_info="contract_info.json"):
    with open(contract_info, "r") as f:
        return json.load(f)


def load_state(source_w3, destination_w3, state_file=STATE_FILE):
    path = Path(state_file)
    if path.exists():
        with open(path, "r") as f:
            return json.load(f)

    return {
        "last_source_block": max(0, source_w3.eth.block_number - 5),
        "last_destination_block": max(0, destination_w3.eth.block_number - 5),
        "processed_source_deposits": [],
        "processed_destination_unwraps": []
    }


def save_state(state, state_file=STATE_FILE):
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2)


def make_event_id(event):
    return f"{event['transactionHash'].hex()}:{event['logIndex']}"


def sign_and_send_tx(w3, tx, private_key):
    signed_tx = w3.eth.account.sign_transaction(tx, private_key=private_key)
    tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
    return receipt.transactionHash.hex()


def build_tx(w3, function_call, sender_address):
    return function_call.build_transaction({
        "from": sender_address,
        "nonce": w3.eth.get_transaction_count(sender_address),
        "gas": 500000,
        "gasPrice": w3.eth.gas_price,
        "chainId": w3.eth.chain_id
    })


def scan_blocks(chain, contract_info="contract_info.json"):
    """
    chain - (string) should be either "source" or "destination"
    Scan the last 5 blocks of the source and destination chains
    Look for 'Deposit' events on the source chain and 'Unwrap' events on the destination chain
    When Deposit events are found on the source chain, call the 'wrap' function the destination chain
    When Unwrap events are found on the destination chain, call the 'withdraw' function on the source chain
    """

    if chain not in ['source', 'destination']:
        print(f"Invalid chain: {chain}")
        return 0

    # Connect to both chains because the bridge must read one side and write to the other
    source_w3 = connect_to("source")
    destination_w3 = connect_to("destination")

    # Load full contract info
    contracts = load_full_contract_info(contract_info)

    private_key = contracts["private_key"]
    sender_address = source_w3.eth.account.from_key(private_key).address

    # Build contract objects
    source_info = contracts["source"]
    destination_info = contracts["destination"]

    source_contract = source_w3.eth.contract(
        address=Web3.to_checksum_address(source_info["address"]),
        abi=source_info["abi"]
    )

    destination_contract = destination_w3.eth.contract(
        address=Web3.to_checksum_address(destination_info["address"]),
        abi=destination_info["abi"]
    )

    # Load persistent state so we do not replay the same events
    state = load_state(source_w3, destination_w3)

    if chain == "source":
        latest_block = source_w3.eth.block_number
        from_block = max(state["last_source_block"] + 1, latest_block - 4)
        to_block = latest_block

        if from_block > to_block:
            print("No new source blocks to scan.")
            return 1

        print(f"Scanning source chain blocks {from_block} to {to_block}")

        deposit_events = source_contract.events.Deposit.get_logs(
            from_block=from_block,
            to_block=to_block
        )

        for event in deposit_events:
            event_id = make_event_id(event)
            if event_id in state["processed_source_deposits"]:
                continue

            underlying_token = event["args"]["token"]
            recipient = event["args"]["recipient"]
            amount = event["args"]["amount"]

            print(
                f"Found Deposit event: token={underlying_token}, "
                f"recipient={recipient}, amount={amount}"
            )

            tx = build_tx(
                destination_w3,
                destination_contract.functions.wrap(
                    underlying_token,
                    recipient,
                    amount
                ),
                sender_address
            )

            try:
                tx_hash = sign_and_send_tx(destination_w3, tx, private_key)
                print(f"wrap() sent on destination chain: {tx_hash}")
                state["processed_source_deposits"].append(event_id)
            except Exception as e:
                print(f"Failed to call wrap(): {e}")

        state["last_source_block"] = to_block

    elif chain == "destination":
        latest_block = destination_w3.eth.block_number
        from_block = max(state["last_destination_block"] + 1, latest_block - 4)
        to_block = latest_block

        if from_block > to_block:
            print("No new destination blocks to scan.")
            return 1

        print(f"Scanning destination chain blocks {from_block} to {to_block}")

        unwrap_events = destination_contract.events.Unwrap.get_logs(
            from_block=from_block,
            to_block=to_block
        )

        for event in unwrap_events:
            event_id = make_event_id(event)
            if event_id in state["processed_destination_unwraps"]:
                continue

            underlying_token = event["args"]["underlying_token"]
            recipient = event["args"]["to"]
            amount = event["args"]["amount"]

            print(
                f"Found Unwrap event: underlying_token={underlying_token}, "
                f"recipient={recipient}, amount={amount}"
            )

            tx = build_tx(
                source_w3,
                source_contract.functions.withdraw(
                    underlying_token,
                    recipient,
                    amount
                ),
                sender_address
            )

            try:
                tx_hash = sign_and_send_tx(source_w3, tx, private_key)
                print(f"withdraw() sent on source chain: {tx_hash}")
                state["processed_destination_unwraps"].append(event_id)
            except Exception as e:
                print(f"Failed to call withdraw(): {e}")

        state["last_destination_block"] = to_block

    save_state(state)
    return 1


if __name__ == "__main__":
    scan_blocks("source")
    scan_blocks("destination")
