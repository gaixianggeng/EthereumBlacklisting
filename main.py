import json
import logging
import sys
from json import JSONDecodeError

import requests
import web3.constants
from typing import Union, List, Tuple, Optional, Callable

from hexbytes import HexBytes
from web3 import Web3
from web3 import constants
from web3._utils.rpc_abi import RPC
from web3.datastructures import AttributeDict
from web3.exceptions import BadFunctionCallOutput, ContractLogicError
from web3.logs import DISCARD
from web3.method import Method, default_root_munger
from web3.types import BlockIdentifier, TxReceiptBlock, RPCEndpoint
from web3.eth import Eth, BaseEth

import database as db
import policy_haircut
import utils
from abis import event_abis, function_abis
from ethereum_utils import EthereumUtils
from policy_poison import PoisonPolicy

import configparser

from utils import format_log_dict

# configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)

# read config.ini
config = configparser.ConfigParser()
config.read("config.ini")
parameters = config["PARAMETERS"]

# read Infura API link from config
remote_provider = Web3.HTTPProvider(parameters["InfuraLink"])

# use default Erigon URL for local provider
local_provider = Web3.HTTPProvider("http://localhost:8545")

# read Etherscan API key from config
ETHERSCAN_API_KEY = parameters["EtherScanKey"]


def get_balance(account: str, block: int):
    if block < 0:
        logging.error(f"Block number cannot be negative (was {block}).")
        return None
    logging.info(f"Getting balance for account {account} at block {block}.")
    try:
        wei = w3_local.eth.get_balance(account, block)
    except ValueError:
        logging.warning(f"World state at block {block} has not been archived and balance cannot be retrieved.")
        return None
    return wei / constants.WEI_PER_ETHER


def print_dict(dictionary):
    for _key in dictionary:
        print(f"'{_key}': {dictionary[_key]}")


def print_logs(receipt):
    for log in receipt["logs"]:
        print_dict(log)
        print("")


def poison_test():
    poison = PoisonPolicy(w3_local)
    poison.add_to_blacklist("0x8C6AE7a05a1dE57582ae2768204276c0ff47ed03", -1, "ETH")

    print(f"Blacklisted amount start: {poison.get_blacklisted_amount(60000) / web3.constants.WEI_PER_ETHER} ETH")

    poison.propagate_blacklist(50000, 1000)

    print(f"Blacklist length: {len(poison.blacklist)}")
    print(f"Blacklisted amount: {poison.get_blacklisted_amount(60000) / web3.constants.WEI_PER_ETHER} ETH")


def is_contract(address: str):
    """
    Check if the given address is a smart contract

    :param address: Ethereum address
    :return: True if smart contract
    """
    return w3.eth.get_code(address).hex() != "0x"


def get_abi(address: str, block: int):
    abi_from_database = database.get_abi(address, block)
    if abi_from_database:
        logging.debug(f"Retrieving ABI for address '{address}' from database.")
        try:
            json.loads(abi_from_database)
        except (TypeError, JSONDecodeError):
            logging.error(f"Decoding ABI from database failed. ABI was: {abi_from_database}")
            exit(-1)
        return abi_from_database
    elif abi_from_database is None:
        return None
    logging.debug(f"Requesting ABI for address '{address}' from EtherScan.")
    api_call = f"https://api.etherscan.io/api?module=contract&apikey={ETHERSCAN_API_KEY}&action=getabi&address={address}"
    response = requests.get(api_call)
    response_json = response.json()
    if "result" in response_json and response_json["result"] != "Contract source code not verified":
        abi = response_json["result"]
    else:
        abi = None
    database.add_contract(address, abi, block)
    return abi


def list_functions_for_contract(address: str, block: int):
    abi = get_abi(address, block)
    if not abi:
        return []
    try:
        function_list = [entry["name"] for entry in json.loads(abi) if entry["type"] == "function"]
    except JSONDecodeError:
        logging.error(f"JSON decoding of ABI failed for address '{address}'. ABI was '{abi}'.")
        return []
    return function_list


def get_contract(address: str, block: int):
    """
    Retrieve the ABI of the given contract address from Etherscan and return a Web3 contract

    :param address: Ethereum address of the contract
    :param block: block at which the last access should be recorded
    :return: web3 Contract object
    """
    abi = get_abi(address, block)
    if not abi:
        return None
    return w3.eth.contract(address=Web3.toChecksumAddress(address), abi=abi)


def get_contract_name_symbol_old(address: str, block: int, force_refresh=False):
    """
    DEPRECATED Get the name and symbol of a smart contract address

    :param force_refresh: stops database check and overwrites already saved data
    :param address: ethereum account address
    :param block: block at which the request was made
    :return: (name, symbol) or None if address is not a contract
    """
    # return from database if already saved
    if not force_refresh:
        db_request = database.get_name_symbol(address, block)
        if db_request:
            return db_request

    if not is_contract(address):
        return None

    # get all functions
    function_list = list_functions_for_contract(address, block)

    # if not supported by contract, use etherscan api
    if "name" not in function_list:
        api_call = f"https://api.etherscan.io/api?module=contract&apikey={ETHERSCAN_API_KEY}&action=getsourcecode&address={address}"
        response = requests.get(api_call)
        response_json = response.json()
        if "result" in response_json:
            if "ContractName" in response_json["result"][0]:
                name = response_json["result"][0]["ContractName"]
                database.set_name_symbol(address, name, None)
                return name, None
            response_json["result"][0]["SourceCode"] = "..."
            response_json["result"][0]["ABI"] = "..."
            logging.warning(f"No name found for contract '{address}'. Response was: {response_json}")
        logging.warning(f"No result received on EtherScan API call. Response was: {response_json}")
        return None, None

    contract = get_contract(address, block)
    name = contract.functions.name().call()

    symbol = None
    if "symbol" in function_list:
        symbol = contract.functions.symbol().call()

    database.set_name_symbol(address, name, symbol)

    return name, symbol


def get_contract_name_symbol(address: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Retrieves the token name and symbol from a token address

    :param address: Ethereum address
    :return: (name, symbol) as string if available, else None for each unavailable field
    """
    name_symbol_abi = function_abis["Name+Symbol"]

    contract = w3.eth.contract(address=Web3.toChecksumAddress(address), abi=name_symbol_abi)

    name = None
    symbol = None

    try:
        name = contract.functions.name().call()
        symbol = contract.functions.symbol().call()
    except web3.exceptions.BadFunctionCallOutput:
        logging.debug(f"Name and/or Symbol for {address} could not be retrieved, since it is not a smart contract.")
    except web3.exceptions.ContractLogicError:
        logging.debug(f"Name and/or Symbol function of smart contract at {address} could does not exist.")

    return name, symbol


def get_invoked_function(transaction_dict: dict = None, transaction_hash: HexBytes = None):
    if transaction_hash:
        transaction_dict = w3.eth.get_transaction(transaction_hash)

    contract_addr = transaction_dict["to"]
    block = transaction_dict["blockNumber"]
    contract = get_contract(contract_addr, block)
    if not contract:
        return None
    try:
        function_input = contract.decode_function_input(transaction_dict["input"])
    except ValueError:
        return None
    function_signature = function_input[0]
    return function_signature


def shutdown():
    """
    Perform cleanup and exit the program

    :return:
    """
    database.cleanup()
    exit(0)


def get_input_data(transaction: Union[AttributeDict, dict], block: int):
    contract_address = transaction["to"]
    contract = get_contract(contract_address, block)
    if not contract:
        return None
    try:
        function_input = contract.decode_function_input(transaction["input"])
    except ValueError:
        return None
    return function_input


def get_swap_path(transaction, block: int):
    input_data = get_input_data(transaction, block)
    if not input_data:
        return None
    currency_list = []
    function_input = input_data[1]
    if "path" not in function_input:
        logging.debug(f"No path found in function input {function_input} for transaction {transaction}.")
        return "[could not be determined]"
    for currency_address in function_input["path"]:
        request = get_contract_name_symbol(currency_address)
        if not request:
            return None
        name, symbol = request
        if symbol:
            currency_list.append(symbol)
        else:
            currency_list.append(name)
    return " -> ".join(currency_list)


def get_swap_tokens(contract_address: str):
    """
    Gets the addresses of the token pair of a DEX smart contract

    :param contract_address: address of the smart contract
    :return: token0, token1 / None, None if an error occurs
    """
    token_functions_abi = function_abis["Tokens"]

    contract = w3.eth.contract(address=Web3.toChecksumAddress(contract_address), abi=token_functions_abi)

    token0 = None
    token1 = None
    try:
        token0 = contract.functions.token0().call({})
        token1 = contract.functions.token1().call({})
    except BadFunctionCallOutput:
        logging.warning(f"token0 or token1 function for DEX contract at {contract_address} could not be executed.")
    except ContractLogicError:
        logging.warning(f"Smart contract at {contract_address} does not support token0 or token1 functions.")

    return token0, token1


def get_transaction_logs(receipt: AttributeDict):
    if not isinstance(receipt, AttributeDict):
        raise ValueError(f"Type {type(receipt)} is not a legal argument for get_transaction_logs.")

    if not isinstance(receipt["blockHash"], HexBytes):
        converted_receipt = format_log_dict(receipt)
        receipt = converted_receipt

    checked_addresses = []
    _log_dict = {}

    for log in receipt["logs"]:
        smart_contract = log["address"]

        if smart_contract in checked_addresses:
            continue

        checked_addresses.append(smart_contract)
        contract_object = get_contract(address=smart_contract, block=test_block)

        if contract_object is None:
            logging.warning(f"No ABI found for address {smart_contract}")
            continue

        receipt_event_signature_hex = Web3.toHex(HexBytes(log["topics"][0]))

        abi_events = [abi for abi in contract_object.abi if abi["type"] == "event"]
        decoded_logs = []

        for event in abi_events:
            name = event["name"]
            inputs = [param["type"] for param in event["inputs"]]
            inputs = ",".join(inputs)
            # Hash event signature
            event_signature_text = f"{name}({inputs})"
            event_signature_hex = Web3.toHex(Web3.keccak(text=event_signature_text))
            # Find match between log's event signature and ABI's event signature
            if event_signature_hex == receipt_event_signature_hex:
                # Decode matching log
                # logging.info(f"Decoding log {receipt}")
                decoded_logs = contract_object.events[event["name"]]().processReceipt(receipt, errors=DISCARD)
                break

        for _processed_log in decoded_logs:
            _log_dict[str(_processed_log["logIndex"])] = _processed_log

    return _log_dict


def haircut_policy_test():
    blacklist_policy = policy_haircut.HaircutPolicy(w3, logging_level=logging.INFO)
    blacklist_policy.add_account_to_blacklist(address="0x11b815efB8f581194ae79006d24E0d814B7697F6", block=test_block)
    blacklist_policy.add_account_to_blacklist(address="0x529fFceC1Ee0DBBB822b29982B7D5ea7B8DcE4E2", block=test_block)
    print(f"Blacklist at start: {blacklist_policy.get_blacklist()}")
    print("Amounts:")
    blacklist_policy.print_blacklisted_amount()

    blacklist_policy.propagate_blacklist(test_block, 100)

    if 1 != 2/1:
        return

    for block in range(test_block, test_block + 11):
        full_block = w3.eth.get_block(block)

        for transaction_log in w3.eth.get_block_receipts(block):
            transaction_log = utils.format_log_dict(transaction_log)

            full_transaction = w3.eth.get_transaction(transaction_log["transactionHash"])

            blacklist_policy.check_transaction(transaction_log, full_transaction, full_block)

    print(f"Final blacklist: {blacklist_policy.get_blacklist()}")
    print(blacklist_policy.get_blacklist_metrics())
    print("Amounts:")
    blacklist_policy.print_blacklisted_amount()


def haircut_policy_test_transaction(tx_hash: str):
    blacklist_policy = policy_haircut.HaircutPolicy(w3, logging_level=logging.DEBUG)
    blacklist_policy.add_account_to_blacklist(address="0x11b815efB8f581194ae79006d24E0d814B7697F6", block=test_block)
    print(blacklist_policy.get_blacklist())

    transaction_log = w3.eth.get_transaction_receipt(tx_hash)

    full_transaction = w3.eth.get_transaction(tx_hash)

    blacklist_policy.check_transaction(transaction_log, full_transaction, None)

    print(f"Blacklist before writing: {blacklist_policy._blacklist}")

    print(f"Blacklist after writing: {blacklist_policy.get_blacklist()}")


if __name__ == '__main__':
    print("")
    logging.info("************ Starting **************")

    eth_getBlockReceipts = RPCEndpoint("eth_getBlockReceipts")


    def get_block_receipts(self, block_identifier: BlockIdentifier) -> List[TxReceiptBlock]:
        return [utils.format_log_dict(log) for log in self._get_block_receipts(block_identifier)]


    _get_block_receipts: Method[Callable[[BlockIdentifier], List[TxReceiptBlock]]] = Method(
        eth_getBlockReceipts,
        mungers=[default_root_munger], )

    setattr(Eth, "get_block_receipts", get_block_receipts)
    setattr(BaseEth, "_get_block_receipts", _get_block_receipts)

    # setup web3
    w3_local = Web3(local_provider)
    w3_remote = Web3(remote_provider)

    # PICK WEB3 PROVIDER
    w3 = w3_local

    # read database location from config and open it
    database = db.Database(parameters["Database"])

    eth_utils = EthereumUtils(w3)

    # get the latest block and log it
    latest_block = w3.eth.get_block_number()
    logging.info(f"Latest block: {latest_block}.")

    # example block and transaction
    test_block = 14394958
    test_tx = "0x7435b60090e0347fc09bb961e02a4dd5baa59ce0ed83de2f0dffca36243d66f9"

    transfer_test_tx = "0xea2ea4fd6a58cecb2de513bdc8448b8079da9df3dfafd7b01a219b30afdc6ecd"

    # ********* TESTING *************

    # transaction_balance_test('0x11b815efB8f581194ae79006d24E0d814B7697F6')
    # more balance test accounts
    '0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D'

    '0x1111111254fb6c44bAC0beD2854e76F90643097d'

    haircut_policy_test()
    # haircut_policy_test_transaction(transfer_test_tx)

    shutdown()
