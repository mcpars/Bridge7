from web3 import Web3
from web3.providers.rpc import HTTPProvider
from web3.middleware import ExtraDataToPOAMiddleware #Necessary for POA chains
from datetime import datetime
import json
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



def scan_blocks(chain, contract_info="contract_info.json"):
    """
        chain - (string) should be either "source" or "destination"
        Scan the last 5 blocks of the source and destination chains
        Look for 'Deposit' events on the source chain and 'Unwrap' events on the destination chain
        When Deposit events are found on the source chain, call the 'wrap' function the destination chain
        When Unwrap events are found on the destination chain, call the 'withdraw' function on the source chain
    """

    # This is different from Bridge IV where chain was "avax" or "bsc"
    if chain not in ['source','destination']:
        print( f"Invalid chain: {chain}" )
        return 0
    
    with open(contract_info, "r") as f:
        full = json.load(f)

    private_key = full.get("private_key")
    acct = Web3().eth.account.from_key(private_key)

    if chain == "source":
        w3 = connect_to("source")
        other_w3 = connect_to("destination")

        source_contract = w3.eth.contract(
            address=Web3.to_checksum_address(full["source"]["address"]),
            abi=full["source"]["abi"]
        )

        dest_contract = other_w3.eth.contract(
            address=Web3.to_checksum_address(full["destination"]["address"]),
            abi=full["destination"]["abi"]
        )

        latest = w3.eth.block_number
        start_block = max(0, latest - 5)

        events = []
        for block_num in range(start_block, latest + 1):
            block_events = source_contract.events.Deposit().get_logs(
                from_block=block_num,
                to_block=block_num
            )
            events.extend(block_events)

        nonce = other_w3.eth.get_transaction_count(acct.address)

        for e in events:
            args = e["args"]

            tx = dest_contract.functions.wrap(
                args["token"],
                args["recipient"],
                args["amount"]
            ).build_transaction({
                "from": acct.address,
                "nonce": nonce,
                "gas": 2000000,
                "gasPrice": other_w3.eth.gas_price,
                "chainId": other_w3.eth.chain_id
            })

            signed = acct.sign_transaction(tx)
            tx_hash = other_w3.eth.send_raw_transaction(signed.raw_transaction)
            other_w3.eth.wait_for_transaction_receipt(tx_hash)
            nonce += 1

    elif chain == "destination":
        w3 = connect_to("destination")
        other_w3 = connect_to("source")

        dest_contract = w3.eth.contract(
            address=Web3.to_checksum_address(full["destination"]["address"]),
            abi=full["destination"]["abi"]
        )

        source_contract = other_w3.eth.contract(
            address=Web3.to_checksum_address(full["source"]["address"]),
            abi=full["source"]["abi"]
        )

        latest = w3.eth.block_number
        start_block = max(0, latest - 5)

        events = []
        for block_num in range(start_block, latest + 1):
            try:
                block_events = dest_contract.events.Unwrap().get_logs(
                    from_block=block_num,
                    to_block=block_num
                )
                events.extend(block_events)
            except Exception as err:
                print(f"Skipping block {block_num}: {err}")

        nonce = other_w3.eth.get_transaction_count(acct.address)

        for e in events:
            args = e["args"]

            tx = source_contract.functions.withdraw(
                args["underlying_token"],
                args["to"],
                args["amount"]
            ).build_transaction({
                "from": acct.address,
                "nonce": nonce,
                "gas": 2000000,
                "gasPrice": other_w3.eth.gas_price,
                "chainId": other_w3.eth.chain_id
            })

            signed = acct.sign_transaction(tx)
            tx_hash = other_w3.eth.send_raw_transaction(signed.raw_transaction)
            other_w3.eth.wait_for_transaction_receipt(tx_hash)
            nonce += 1

    return 1
