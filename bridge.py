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

def _get_private_key(contracts):
    """
    Try a few likely field names for the bridge warden private key.
    Put one of these in contract_info.json at top level or inside each chain entry.
    """
    candidate_keys = [
        "private_key",
        "warden_private_key",
        "deployer_private_key",
        "signing_key",
        "key",
    ]

    for k in candidate_keys:
        if k in contracts:
            return contracts[k]

    for side in ["source", "destination"]:
        if side in contracts and isinstance(contracts[side], dict):
            for k in candidate_keys:
                if k in contracts[side]:
                    return contracts[side][k]

    raise KeyError("No private key found in contract_info.json")


def _load_all_contract_data(contract_info_file):
    with open(contract_info_file, "r") as f:
        return json.load(f)


def _build_signed_tx(w3, tx, private_key):
    signed = w3.eth.account.sign_transaction(tx, private_key=private_key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    return tx_hash.hex()


def _process_source_deposits(w3_source, w3_dest, source_contract, dest_contract, private_key):
    latest = w3_source.eth.block_number
    from_block = max(0, latest - 5)

    deposit_events = source_contract.events.Deposit().get_logs(
        from_block=from_block,
        to_block=latest
    )

    sender = w3_dest.eth.account.from_key(private_key).address
    chain_id = w3_dest.eth.chain_id
    nonce = w3_dest.eth.get_transaction_count(sender)

    tx_hashes = []

    for event in deposit_events:
        token = event["args"]["token"]
        recipient = event["args"]["recipient"]
        amount = event["args"]["amount"]

        tx = dest_contract.functions.wrap(
            token,
            recipient,
            amount
        ).build_transaction({
            "from": sender,
            "nonce": nonce,
            "chainId": chain_id,
            "gasPrice": w3_dest.eth.gas_price,
        })

        # estimate gas if possible
        try:
            tx["gas"] = w3_dest.eth.estimate_gas(tx)
        except Exception:
            tx["gas"] = 300000

        tx_hashes.append(_build_signed_tx(w3_dest, tx, private_key))
        nonce += 1

    return tx_hashes


def _process_destination_unwraps(w3_dest, w3_source, dest_contract, source_contract, private_key):
    latest = w3_dest.eth.block_number
    from_block = max(0, latest - 5)

    unwrap_events = dest_contract.events.Unwrap().get_logs(
        from_block=from_block,
        to_block=latest
    )

    sender = w3_source.eth.account.from_key(private_key).address
    chain_id = w3_source.eth.chain_id
    nonce = w3_source.eth.get_transaction_count(sender)

    tx_hashes = []

    for event in unwrap_events:
        underlying_token = event["args"]["underlying_token"]
        recipient = event["args"]["to"]
        amount = event["args"]["amount"]

        tx = source_contract.functions.withdraw(
            underlying_token,
            recipient,
            amount
        ).build_transaction({
            "from": sender,
            "nonce": nonce,
            "chainId": chain_id,
            "gasPrice": w3_source.eth.gas_price,
        })

        try:
            tx["gas"] = w3_source.eth.estimate_gas(tx)
        except Exception:
            tx["gas"] = 300000

        tx_hashes.append(_build_signed_tx(w3_source, tx, private_key))
        nonce += 1

    return tx_hashes


def scan_blocks(chain, contract_info="contract_info.json"):
    """
    chain - should be either "source" or "destination"

    Scan the last 5 blocks:
    - On source: look for Deposit and call wrap() on destination
    - On destination: look for Unwrap and call withdraw() on source
    """
    if chain not in ["source", "destination"]:
        print(f"Invalid chain: {chain}")
        return 0

    contracts = _load_all_contract_data(contract_info)
    private_key = _get_private_key(contracts)

    source_info = contracts["source"]
    dest_info = contracts["destination"]

    w3_source = connect_to("source")
    w3_dest = connect_to("destination")

    source_contract = w3_source.eth.contract(
        address=Web3.to_checksum_address(source_info["address"]),
        abi=source_info["abi"]
    )
    dest_contract = w3_dest.eth.contract(
        address=Web3.to_checksum_address(dest_info["address"]),
        abi=dest_info["abi"]
    )

    if chain == "source":
        return _process_source_deposits(
            w3_source, w3_dest, source_contract, dest_contract, private_key
        )

    if chain == "destination":
        return _process_destination_unwraps(
            w3_dest, w3_source, dest_contract, source_contract, private_key
        )
