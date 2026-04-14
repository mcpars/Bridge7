from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware
from pathlib import Path
import json

STATE_FILE = "bridge_state.json"


def connect_to(chain):
    if chain == 'source':
        api_url = "https://api.avax-test.network/ext/bc/C/rpc"
    elif chain == 'destination':
        api_url = "https://data-seed-prebsc-1-s1.binance.org:8545/"
    else:
        raise ValueError(f"Invalid chain: {chain}")

    w3 = Web3(Web3.HTTPProvider(api_url))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    return w3


def get_contract_info(chain, contract_info):
    """
    Load the contract_info file into a dictionary.
    This function is used by the autograder and may also be useful directly.
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


def load_state(state_file=STATE_FILE):
    path = Path(state_file)
    if path.exists():
        with open(path, "r") as f:
            return json.load(f)

    return {
        "processed_source_deposits": [],
        "processed_destination_unwraps": []
    }


def save_state(state, state_file=STATE_FILE):
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2)


def make_event_id_from_log(log):
    return f"{log['transactionHash'].hex()}:{log['logIndex']}"


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


def decode_contract_events_from_block(w3, contract, block_num):
    events = []
    block = w3.eth.get_block(block_num, full_transactions=True)
    contract_address = Web3.to_checksum_address(contract.address)

    for tx in block.transactions:
        try:
            receipt = w3.eth.get_transaction_receipt(tx["hash"])
        except Exception:
            continue

        for log in receipt["logs"]:
            try:
                if Web3.to_checksum_address(log["address"]) != contract_address:
                    continue
            except Exception:
                continue

            for event_name in ["Deposit", "Unwrap"]:
                if not hasattr(contract.events, event_name):
                    continue

                event_cls = getattr(contract.events, event_name)
                try:
                    decoded = event_cls().process_log(log)
                    events.append(decoded)
                    break
                except Exception:
                    pass

    return events


def scan_blocks(chain, contract_info="contract_info.json"):
    """
    chain - should be either "source" or "destination"

    Scans recent blocks on the requested chain:
    - source: look for Deposit events and call wrap() on destination
    - destination: look for Unwrap events and call withdraw() on source
    """

    if chain not in ['source', 'destination']:
        print(f"Invalid chain: {chain}")
        return 0

    source_w3 = connect_to("source")
    destination_w3 = connect_to("destination")

    contracts = load_full_contract_info(contract_info)
    private_key = contracts["private_key"]
    sender_address = source_w3.eth.account.from_key(private_key).address

    source_contract = source_w3.eth.contract(
        address=Web3.to_checksum_address(contracts["source"]["address"]),
        abi=contracts["source"]["abi"]
    )

    destination_contract = destination_w3.eth.contract(
        address=Web3.to_checksum_address(contracts["destination"]["address"]),
        abi=contracts["destination"]["abi"]
    )

    state = load_state()

    if chain == "source":
        latest_block = source_w3.eth.block_number
        from_block = max(0, latest_block - 20)
        to_block = latest_block

        print(f"Scanning source chain blocks {from_block} to {to_block}")

        for block_num in range(from_block, to_block + 1):
            try:
                deposit_events = source_contract.events.Deposit.get_logs(
                    from_block=block_num,
                    to_block=block_num
                )
            except Exception as e:
                print(f"Failed to fetch Deposit logs for block {block_num}: {e}")
                continue

            for event in deposit_events:
                event_id = f"{event['transactionHash'].hex()}:{event['logIndex']}"
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

    elif chain == "destination":
        latest_block = destination_w3.eth.block_number
        from_block = max(0, latest_block - 20)
        to_block = latest_block

        print(f"Scanning destination chain blocks {from_block} to {to_block}")

        for block_num in range(from_block, to_block + 1):
            try:
                decoded_events = decode_contract_events_from_block(
                    destination_w3,
                    destination_contract,
                    block_num
                )
            except Exception as e:
                print(f"Failed to inspect block {block_num}: {e}")
                continue

            for event in decoded_events:
                if event["event"] != "Unwrap":
                    continue

                event_id = make_event_id_from_log(event)
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

    save_state(state)
    return 1
