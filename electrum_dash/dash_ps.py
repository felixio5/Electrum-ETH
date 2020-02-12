import asyncio
import copy
import logging
import re
import random
import time
import threading
from bls_py import bls
from enum import IntEnum
from collections import defaultdict, deque
from decimal import Decimal
from math import ceil
from uuid import uuid4

from . import constants
from .bitcoin import (COIN, TYPE_ADDRESS, TYPE_SCRIPT, address_to_script,
                      is_address)
from .dash_tx import (STANDARD_TX, PSTxTypes, SPEC_TX_NAMES, PSCoinRounds,
                      str_ip, CTxIn, CTxOut)
from .dash_msg import (DSPoolStatusUpdate, DSMessageIDs, ds_msg_str,
                       ds_pool_state_str, DashDsaMsg, DashDsiMsg, DashDssMsg,
                       PRIVATESEND_ENTRY_MAX_SIZE)
from .keystore import xpubkey_to_address
from .logging import Logger, root_logger
from .transaction import Transaction, TxOutput
from .util import (NoDynamicFeeEstimates, log_exceptions, SilentTaskGroup,
                   NotEnoughFunds, bfh, is_android, profiler)
from .i18n import _


TXID_PATTERN = re.compile('([0123456789ABCDEFabcdef]{64})')
ADDR_PATTERN = re.compile(
    '([123456789ABCDEFGHJKLMNPQRSTUVWXYZ'
    'abcdefghijkmnopqrstuvwxyz]{20,80})')
FILTERED_TXID = '<filtered txid>'
FILTERED_ADDR = '<filtered address>'


def filter_log_line(line):
    pos = 0
    output_line = ''
    while pos < len(line):
        m = TXID_PATTERN.search(line, pos)
        if m:
            output_line += line[pos:m.start()]
            output_line += FILTERED_TXID
            pos = m.end()
            continue

        m = ADDR_PATTERN.search(line, pos)
        if m:
            addr = m.group()
            if is_address(addr, net=constants.net):
                output_line += line[pos:m.start()]
                output_line += FILTERED_ADDR
                pos = m.end()
                continue

        output_line += line[pos:]
        break
    return output_line


def to_duffs(amount):
    return round(Decimal(amount)*COIN)


def sort_utxos_by_ps_rounds(x):
    ps_rounds = x['ps_rounds']
    if ps_rounds is None:
        return PSCoinRounds.MINUSINF
    return ps_rounds


class PSDenoms(IntEnum):
    D10 = 1
    D1 = 2
    D0_1 = 4
    D0_01 = 8
    D0_001 = 16


PS_DENOMS_DICT = {
    to_duffs(10.0001): PSDenoms.D10,
    to_duffs(1.00001): PSDenoms.D1,
    to_duffs(0.100001): PSDenoms.D0_1,
    to_duffs(0.0100001): PSDenoms.D0_01,
    to_duffs(0.00100001): PSDenoms.D0_001,
}


PS_DENOM_REVERSE_DICT = {int(v): k for k, v in PS_DENOMS_DICT.items()}


COLLATERAL_VAL = to_duffs(0.0001)
CREATE_COLLATERAL_VAL = COLLATERAL_VAL*4
PS_DENOMS_VALS = sorted(PS_DENOMS_DICT.keys())

PS_MIXING_TX_TYPES = list(map(lambda x: x.value, [PSTxTypes.NEW_DENOMS,
                                                  PSTxTypes.NEW_COLLATERAL,
                                                  PSTxTypes.PAY_COLLATERAL,
                                                  PSTxTypes.DENOMINATE]))

PS_SAVED_TX_TYPES = list(map(lambda x: x.value, [PSTxTypes.NEW_DENOMS,
                                                 PSTxTypes.NEW_COLLATERAL,
                                                 PSTxTypes.PAY_COLLATERAL,
                                                 PSTxTypes.DENOMINATE,
                                                 PSTxTypes.PRIVATESEND,
                                                 PSTxTypes.SPEND_PS_COINS,
                                                 PSTxTypes.OTHER_PS_COINS]))

DEFAULT_KEEP_AMOUNT = 2
MIN_KEEP_AMOUNT = 2
MAX_KEEP_AMOUNT = int(1e9)

DEFAULT_MIX_ROUNDS = 4
MIN_MIX_ROUNDS = 2
MAX_MIX_ROUNDS = 16
MAX_MIX_ROUNDS_TESTNET = 256

DEFAULT_PRIVATESEND_SESSIONS = 4
MIN_PRIVATESEND_SESSIONS = 1
MAX_PRIVATESEND_SESSIONS = 10

DEFAULT_GROUP_HISTORY = True
DEFAULT_NOTIFY_PS_TXS = False
DEFAULT_SUBSCRIBE_SPENT = False

POOL_MIN_PARTICIPANTS = 3
POOL_MAX_PARTICIPANTS = 5

PRIVATESEND_QUEUE_TIMEOUT = 30
PRIVATESEND_SESSION_MSG_TIMEOUT = 40

WAIT_FOR_MN_TXS_TIME_SEC = 120

# PSManager states
class PSStates(IntEnum):
    Unsupported = 0
    Disabled = 1
    Initializing = 2
    Ready = 3
    StartMixing = 4
    Mixing = 5
    StopMixing = 6
    FindingUntracked = 7
    Errored = 8

# Keypairs cache types
KP_SPENDABLE = 'spendable'          # regular utxos
KP_PS_SPENDABLE = 'ps_spendable'    # ps_denoms/ps_collateral utxos
KP_PS_COINS = 'ps_coins'            # output addressess for denominate tx
KP_PS_CHANGE = 'ps_change'          # output addressess for pay collateral tx
KP_ALL_TYPES = [KP_SPENDABLE, KP_PS_SPENDABLE, KP_PS_COINS, KP_PS_CHANGE]

# Keypairs cache states
class KPStates(IntEnum):
    Empty = 0
    NeedGen = 1
    Generating = 2
    SpendableDone = 3
    PSSpendableDone = 4
    PSChangeDone = 5
    AllDone = 6
    Cleaning = 7

# Keypairs cleanup timeout when mixing is stopped
DEFAULT_KP_TIMEOUT = 0
MIN_KP_TIMEOUT = 0
MAX_KP_TIMEOUT = 5


class PSTxData:
    '''
    uuid: unique id for addresses reservation
    tx_type: PSTxTypes type
    txid: tx hash
    raw_tx: raw tx data
    sent: time when tx was sent to network
    next_send: minimal time when next send attempt should occur
    '''

    __slots__ = 'uuid tx_type txid raw_tx sent next_send'.split()

    def __init__(self, **kwargs):
        for k in self.__slots__:
            if k in kwargs:
                if k == 'tx_type':
                    setattr(self, k, int(kwargs[k]))
                else:
                    setattr(self, k, kwargs[k])
            else:
                setattr(self, k, None)

    def _as_dict(self):
        '''return dict txid -> (uuid, sent, next_send, tx_type, raw_tx)'''
        return {self.txid: (self.uuid, self.sent, self.next_send,
                            self.tx_type, self.raw_tx)}

    @classmethod
    def _from_txid_and_tuple(cls, txid, data_tuple):
        '''
        New instance from txid
        and (uuid, sent, next_send, tx_type, raw_tx) tuple
        '''
        uuid, sent, next_send, tx_type, raw_tx = data_tuple
        return cls(uuid=uuid, txid=txid, raw_tx=raw_tx,
                   tx_type=tx_type, sent=sent, next_send=next_send)

    def __eq__(self, other):
        if type(other) != PSTxData:
            return False
        if id(self) == id(other):
            return True
        for k in self.__slots__:
            if getattr(self, k) != getattr(other, k):
                return False
        return True

    async def send(self, psman, ignore_next_send=False):
        err = ''
        if self.sent:
            return False, err
        now = time.time()
        if not ignore_next_send:
            next_send = self.next_send
            if next_send and next_send > now:
                return False, err
        try:
            tx = Transaction(self.raw_tx)
            await psman.network.broadcast_transaction(tx)
            self.sent = time.time()
            return True, err
        except Exception as e:
            err = str(e)
            self.next_send = now + 10
            return False, err


class PSTxWorkflow:
    '''
    uuid: unique id for addresses reservation
    completed: workflow creation completed
    tx_data: txid -> PSTxData
    tx_order: creation order of workflow txs
    '''

    __slots__ = 'uuid completed tx_data tx_order'.split()

    def __init__(self, **kwargs):
        uuid = kwargs.pop('uuid', None)
        if uuid is None:
            raise TypeError('missing required uuid argument')
        self.uuid = uuid
        self.completed = kwargs.pop('completed', False)
        self.tx_order = kwargs.pop('tx_order', [])[:]  # copy
        tx_data = kwargs.pop('tx_data', {})
        self.tx_data = {}  # copy
        for txid, v in tx_data.items():
            if type(v) in (tuple, list):
                self.tx_data[txid] = PSTxData._from_txid_and_tuple(txid, v)
            else:
                self.tx_data[txid] = v

    @property
    def lid(self):
        return self.uuid[:8] if self.uuid else self.uuid

    def _as_dict(self):
        '''return dict with keys from __slots__ and corresponding values'''
        tx_data = {}  # copy
        for v in self.tx_data.values():
            tx_data.update(v._as_dict())
        return {
            'uuid': self.uuid,
            'completed': self.completed,
            'tx_data': tx_data,
            'tx_order': self.tx_order[:],  # copy
        }

    @classmethod
    def _from_dict(cls, data_dict):
        return cls(**data_dict)

    def __eq__(self, other):
        if type(other) != PSTxWorkflow:
            return False
        elif id(self) == id(other):
            return True
        elif self.uuid != other.uuid:
            return False
        elif self.completed != other.completed:
            return False
        elif self.tx_order != other.tx_order:
            return False
        elif set(self.tx_data.keys()) != set(other.tx_data.keys()):
            return False
        for k in self.tx_data.keys():
            if self.tx_data[k] != other.tx_data[k]:
                return False
        else:
            return True

    def next_to_send(self, wallet):
        for txid in self.tx_order:
            tx_data = self.tx_data[txid]
            if not tx_data.sent and wallet.is_local_tx(txid):
                return tx_data

    def add_tx(self, **kwargs):
        txid = kwargs.pop('txid')
        raw_tx = kwargs.pop('raw_tx', None)
        tx_type = kwargs.pop('tx_type')
        if not txid or not tx_type:
            return
        tx_data = PSTxData(uuid=self.uuid, txid=txid,
                           raw_tx=raw_tx, tx_type=tx_type)
        self.tx_data[txid] = tx_data
        self.tx_order.append(txid)
        return tx_data

    def pop_tx(self, txid):
        if txid in self.tx_data:
            res = self.tx_data.pop(txid)
        else:
            res = None
        self.tx_order = [tid for tid in self.tx_order if tid != txid]
        return res


class PSDenominateWorkflow:
    '''
    uuid: unique id for spending denoms reservation
    denom: workflow denom value
    rounds: workflow inputs mix rounds
    inputs: list of spending denoms outpoints
    outputs: list of reserved output addresses
    completed: time when dsc message received
    '''

    __slots__ = 'uuid denom rounds inputs outputs completed'.split()

    def __init__(self, **kwargs):
        uuid = kwargs.pop('uuid', None)
        if uuid is None:
            raise TypeError('missing required uuid argument')
        self.uuid = uuid
        self.denom = kwargs.pop('denom', 0)
        self.rounds = kwargs.pop('rounds', 0)
        self.inputs = kwargs.pop('inputs', [])[:]  # copy
        self.outputs = kwargs.pop('outputs', [])[:]  # copy
        self.completed = kwargs.pop('completed', 0)

    @property
    def lid(self):
        return self.uuid[:8] if self.uuid else self.uuid

    def _as_dict(self):
        '''return dict uuid -> (denom, rounds, inputs, outputs, completed)'''
        return {
            self.uuid: (
                self.denom,
                self.rounds,
                self.inputs[:],  # copy
                self.outputs[:],  # copy
                self.completed,
            )
        }

    @classmethod
    def _from_uuid_and_tuple(cls, uuid, data_tuple):
        '''New from uuid, (denom, rounds, inputs, outputs, completed) tuple'''
        denom, rounds, inputs, outputs, completed = data_tuple[:5]
        return cls(uuid=uuid, denom=denom, rounds=rounds,
                   inputs=inputs[:], outputs=outputs[:],  # copy
                   completed=completed)

    def __eq__(self, other):
        if type(other) != PSDenominateWorkflow:
            return False
        elif id(self) == id(other):
            return True
        elif self.uuid != other.uuid:
            return False
        elif self.denom != other.denom:
            return False
        elif self.rounds != other.rounds:
            return False
        elif self.inputs != other.inputs:
            return False
        elif self.outputs != other.outputs:
            return False
        elif self.completed != other.completed:
            return False
        else:
            return True


class PSMinRoundsCheckFailed(Exception):
    """Thrown when check for coins minimum mixing rounds failed"""


class PSPossibleDoubleSpendError(Exception):
    """Thrown when trying to broadcast recently used ps denoms/collateral"""


class PSSpendToPSAddressesError(Exception):
    """Thrown when trying to broadcast tx with ps coins spent to ps addrs"""


class NotFoundInKeypairs(Exception):
    """Thrown when output address not found in keypairs cache"""

class SignWithKeypairsFailed(Exception):
    """Thrown when transaction signing with keypairs reserved failed"""

class AddPSDataError(Exception):
    """Thrown when failed _add_*_ps_data method"""


class RmPSDataError(Exception):
    """Thrown when failed _rm_*_ps_data method"""


class PSMixSession:

    def __init__(self, psman, denom_value, denom, dsq, wfl_lid):
        self.logger = psman.logger
        self.denom_value = denom_value
        self.denom = denom
        self.wfl_lid = wfl_lid

        network = psman.wallet.network
        self.dash_net = network.dash_net
        self.mn_list = network.mn_list

        self.dash_peer = None
        self.sml_entry = None

        if dsq:
            outpoint = str(dsq.masternodeOutPoint)
            self.sml_entry = self.mn_list.get_mn_by_outpoint(outpoint)
        if not self.sml_entry:
            try_cnt = 0
            while True:
                try_cnt += 1
                self.sml_entry = self.mn_list.get_random_mn()
                if self.peer_str not in psman.recent_mixes_mns:
                    break
                if try_cnt >= 10:
                    raise Exception('Can not select random'
                                    ' not recently used  MN')
        if not self.sml_entry:
            raise Exception('No SML entries found')
        psman.recent_mixes_mns.append(self.peer_str)
        self.msg_queue = asyncio.Queue()

        self.session_id = 0
        self.state = None
        self.msg_id = None
        self.entries_count = 0
        self.masternodeOutPoint = None
        self.fReady = False
        self.nTime = 0
        self.start_time = time.time()

    @property
    def peer_str(self):
        return f'{str_ip(self.sml_entry.ipAddress)}:{self.sml_entry.port}'

    async def run_peer(self):
        if self.dash_peer:
            raise Exception('Sesions already have running DashPeer')
        self.dash_peer = await self.dash_net.run_mixing_peer(self.peer_str,
                                                             self.sml_entry,
                                                             self)
        if not self.dash_peer:
            raise Exception(f'Peer {self.peer_str} connection failed')
        self.logger.info(f'Started mixing session for {self.wfl_lid},'
                         f' peer: {self.peer_str}, denom={self.denom_value}'
                         f' (nDenom={self.denom})')

    def close_peer(self):
        if not self.dash_peer:
            return
        self.dash_peer.close()
        self.logger.info(f'Stopped mixing session for {self.wfl_lid},'
                         f' peer: {self.peer_str}')

    def verify_ds_msg_sig(self, ds_msg):
        if not self.sml_entry:
            return False
        mn_pub_key = self.sml_entry.pubKeyOperator
        pubk = bls.PublicKey.from_bytes(mn_pub_key)
        sig = bls.Signature.from_bytes(ds_msg.vchSig)
        msg_hash = ds_msg.msg_hash()
        aggr_info = bls.AggregationInfo.from_msg_hash(pubk, msg_hash)
        sig.set_aggregation_info(aggr_info)
        return bls.BLS.verify(sig)

    def verify_final_tx(self, tx, denominate_wfl):
        inputs = denominate_wfl.inputs
        outputs = denominate_wfl.outputs
        icnt = 0
        ocnt = 0
        for i in tx.inputs():
            prev_h = i['prevout_hash']
            prev_n = i['prevout_n']
            if f'{prev_h}:{prev_n}' in inputs:
                icnt += 1
        for o in tx.outputs():
            if o.address in outputs:
                ocnt += 1
        if icnt == len(inputs) and ocnt == len(outputs):
            return True
        else:
            return False

    async def send_dsa(self, pay_collateral_tx):
        msg = DashDsaMsg(self.denom, pay_collateral_tx)
        await self.dash_peer.send_msg('dsa', msg.serialize())
        self.logger.debug(f'{self.wfl_lid}: dsa sent')

    async def send_dsi(self, inputs, pay_collateral_tx, outputs):
        scriptSig = b''
        sequence = 0xffffffff
        vecTxDSIn = []
        for i in inputs:
            prev_h, prev_n = i.split(':')
            prev_h = bfh(prev_h)[::-1]
            prev_n = int(prev_n)
            vecTxDSIn.append(CTxIn(prev_h, prev_n, scriptSig, sequence))
        vecTxDSOut = []
        for o in outputs:
            scriptPubKey = bfh(address_to_script(o))
            vecTxDSOut.append(CTxOut(self.denom_value, scriptPubKey))
        msg = DashDsiMsg(vecTxDSIn, pay_collateral_tx, vecTxDSOut)
        await self.dash_peer.send_msg('dsi', msg.serialize())
        self.logger.debug(f'{self.wfl_lid}: dsi sent')

    async def send_dss(self, signed_inputs):
        msg = DashDssMsg(signed_inputs)
        await self.dash_peer.send_msg('dss', msg.serialize())

    async def read_next_msg(self, denominate_wfl, timeout=None):
        '''Read next msg from msg_queue, process and return (cmd, res) tuple'''
        try:
            if timeout is None:
                timeout = PRIVATESEND_SESSION_MSG_TIMEOUT
            res = await asyncio.wait_for(self.msg_queue.get(), timeout)
        except asyncio.TimeoutError:
            raise Exception('Session Timeout, Reset')
        if not res:  # dash_peer is closed
            raise Exception('peer connection closed')
        elif type(res) == Exception:
            raise res
        cmd = res.cmd
        payload = res.payload
        if cmd == 'dssu':
            res = self.on_dssu(payload)
            return cmd, res
        elif cmd == 'dsq':
            self.logger.debug(f'{self.wfl_lid}: dsq read: {payload}')
            res = self.on_dsq(payload)
            return cmd, res
        elif cmd == 'dsf':
            self.logger.debug(f'{self.wfl_lid}: dsf read: {payload}')
            res = self.on_dsf(payload, denominate_wfl)
            return cmd, res
        elif cmd == 'dsc':
            self.logger.wfl_ok(f'{self.wfl_lid}: dsc read: {payload}')
            res = self.on_dsc(payload)
            return cmd, res
        else:
            self.logger.debug(f'{self.wfl_lid}: unknown msg read, cmd: {cmd}')
            return None, None

    def on_dssu(self, dssu):
        session_id = dssu.sessionID
        if not self.session_id:
            if session_id:
                self.session_id = session_id

        if self.session_id != session_id:
            raise Exception(f'Wrong session id {session_id},'
                            f' was {self.session_id}')

        self.state = dssu.state
        self.msg_id = dssu.messageID
        self.entries_count = dssu.entriesCount

        state = ds_pool_state_str(self.state)
        msg = ds_msg_str(self.msg_id)
        if (dssu.statusUpdate == DSPoolStatusUpdate.ACCEPTED
                and dssu.messageID != DSMessageIDs.ERR_QUEUE_FULL):
            self.logger.debug(f'{self.wfl_lid}: dssu read:'
                              f' state={state}, msg={msg},'
                              f' entries_count={self.entries_count}')
        elif dssu.statusUpdate == DSPoolStatusUpdate.ACCEPTED:
            raise Exception('MN queue is full')
        elif dssu.statusUpdate == DSPoolStatusUpdate.REJECTED:
            raise Exception(f'Get reject status update from MN: {msg}')
        else:
            raise Exception(f'Unknown dssu statusUpdate: {dssu.statusUpdate}')

    def on_dsq(self, dsq):
        denom = dsq.nDenom
        if denom != self.denom:
            raise Exception(f'Wrong denom in dsq msg: {denom},'
                            f' session denom is {self.denom}.')
        # signature verified in dash_peer on receiving dsq message for session
        # signature not verifed for dsq with fReady not set (go to recent dsq)
        if not dsq.fReady:  # additional check
            raise Exception(f'Get dsq with fReady not set')
        if self.fReady:
            raise Exception(f'Another dsq on session with fReady set')
        self.masternodeOutPoint = dsq.masternodeOutPoint
        self.fReady = dsq.fReady
        self.nTime = dsq.nTime

    def on_dsf(self, dsf, denominate_wfl):
        session_id = dsf.sessionID
        if self.session_id != session_id:
            raise Exception(f'Wrong session id {session_id},'
                            f' was {self.session_id}')
        if not self.verify_final_tx(dsf.txFinal, denominate_wfl):
            raise Exception(f'Wrong txFinal')
        return dsf.txFinal

    def on_dsc(self, dsc):
        session_id = dsc.sessionID
        if self.session_id != session_id:
            raise Exception(f'Wrong session id {session_id},'
                            f' was {self.session_id}')
        msg_id = dsc.messageID
        if msg_id != DSMessageIDs.MSG_SUCCESS:
            raise Exception(ds_msg_str(msg_id))


class PSLogSubCat(IntEnum):
    NoCategory = 0
    WflOk = 1
    WflErr = 2
    WflDone = 3


class PSManLogAdapter(logging.LoggerAdapter):

    def __init__(self, logger, extra):
        super(PSManLogAdapter, self).__init__(logger, extra)

    def process(self, msg, kwargs):
        msg, kwargs = super(PSManLogAdapter, self).process(msg, kwargs)
        subcat = kwargs.pop('subcat', None)
        if subcat:
            kwargs['extra']['subcat'] = subcat
        else:
            kwargs['extra']['subcat'] = PSLogSubCat.NoCategory
        return msg, kwargs

    def wfl_done(self, msg, *args, **kwargs):
        self.info(msg, *args, **kwargs, subcat=PSLogSubCat.WflDone)

    def wfl_ok(self, msg, *args, **kwargs):
        self.info(msg, *args, **kwargs, subcat=PSLogSubCat.WflOk)

    def wfl_err(self, msg, *args, **kwargs):
        self.info(msg, *args, **kwargs, subcat=PSLogSubCat.WflErr)


class PSGUILogHandler(logging.Handler):
    '''Write log to maxsize limited queue'''

    def __init__(self, psman):
        super(PSGUILogHandler, self).__init__()
        self.shortcut = psman.LOGGING_SHORTCUT
        self.psman = psman
        self.psman_id = id(psman)
        self.head = 0
        self.tail = 0
        self.log = dict()
        self.setLevel(logging.INFO)
        psman.logger.addHandler(self)
        self.notify = False

    def handle(self, record):
        if record.psman_id != self.psman_id:
            return False
        self.log[self.tail] = record
        self.tail += 1
        if self.tail - self.head > 1000:
            self.clear_log(100)
        if self.notify:
            self.psman.postpone_notification('ps-log-changes', self.psman)
        return True

    def clear_log(self, count=0):
        head = self.head
        if not count:
            count = self.tail - head
        for i in range(head, head+count):
            self.log.pop(i, None)
        self.head = head + count
        if self.notify:
            self.psman.postpone_notification('ps-log-changes', self.psman)


class PSManager(Logger):
    '''Class representing wallet PrivateSend manager'''

    LOGGING_SHORTCUT = 'A'
    NOT_FOUND_KEYS_MSG = _('Insufficient keypairs cached to continue mixing.'
                           ' You can restart mixing to reserve more keyparis')
    SIGN_WIHT_KP_FAILED_MSG = _('Sign with keypairs failed.')
    ADD_PS_DATA_ERR_MSG = _('Error on adding PrivateSend transaction data.')
    SPEND_TO_PS_ADDRS_MSG = _('For privacy reasons blocked attempt to'
                              ' transfer coins to PrivateSend address.')
    ALL_MIXED_MSG = _('PrivateSend mixing is done')
    CLEAR_PS_DATA_MSG = _('Are you sure to clear all wallet PrivateSend data?'
                          ' This is not recommended if there is'
                          ' no particular need.')
    NO_NETWORK_MSG = _('Can not start mixing. Network is not available')
    NO_DASH_NET_MSG = _('Can not start mixing. DashNet is not available')
    LLMQ_DATA_NOT_READY = _('LLMQ quorums data is not fully loaded.'
                            ' Please try again soon.')
    MNS_DATA_NOT_READY = _('Masternodes data is not fully loaded.'
                           ' Please try again soon.')
    NOT_ENABLED_MSG = _('PrivateSend mixing is not enabled')
    INITIALIZING_MSG = _('PrivateSend mixing is initializing.'
                         ' Please try again soon')
    ALREADY_RUN_MSG = _('PrivateSend mixing is already run.')
    FIND_UNTRACKED_RUN_MSG = _('PrivateSend mixing can not start. Process of'
                               ' finding untracked PS transactions'
                               ' is currently run')
    ERRORED_MSG = _('PrivateSend mixing can not start.'
                    ' Please check errors in PS Log tab')
    UNKNOWN_STATE_MSG = _('PrivateSend mixing can not start.'
                          ' Unknown state: {}')
    WALLET_PASSWORD_SET_MSG = _('Wallet password has set. Need to restart'
                                ' mixing for generating keypairs cache')
    if is_android():
        NO_DYNAMIC_FEE_MSG = _('{}\n\nYou can switch fee estimation method'
                               ' on send screen')
    else:
        NO_DYNAMIC_FEE_MSG = _('{}\n\nYou can switch to static fee estimation'
                               ' on Fees Preferences tab')

    def __init__(self, wallet):
        Logger.__init__(self)
        self.log_handler = PSGUILogHandler(self)
        self.logger = PSManLogAdapter(self.logger, {'psman_id': id(self)})

        self.state_lock = threading.Lock()
        self.states = s = PSStates
        self.mixing_running_states = [s.StartMixing, s.Mixing, s.StopMixing]
        self.no_clean_history_states = [s.Initializing, s.Errored,
                                        s.StartMixing, s.Mixing, s.StopMixing,
                                        s.FindingUntracked]
        self.wallet = wallet
        self.config = None
        self._state = PSStates.Unsupported
        self.wallet_types_supported = ['standard']
        if wallet.wallet_type in self.wallet_types_supported:
            if wallet.db.get_ps_data('ps_enabled', False):
                self.state = PSStates.Initializing
            else:
                self.state = PSStates.Disabled
        if self.unsupported:
            supported_str = ', '.join(self.wallet_types_supported)
            this_type = wallet.wallet_type
            self.unsupported_msg = _(f'PrivateSend is currently supported on'
                                     f' next wallet types: {supported_str}.'
                                     f'\n\nThis wallet has type: {this_type}.')
        else:
            self.unsupported_msg  = ''

        self.network = None
        self.dash_net = None
        self.loop = None
        self._loop_thread = None
        self.main_taskgroup = None

        self.keypairs_state_lock = threading.Lock()
        self._keypairs_state = KPStates.Empty
        self._keypairs_cache = {}

        self.callback_lock = threading.Lock()
        self.callbacks = defaultdict(list)

        self.mix_sessions_lock = asyncio.Lock()
        self.mix_sessions = {}  # dict peer -> PSMixSession
        self.recent_mixes_mns = deque([], 16)  # added from mixing sessions

        self.denoms_lock = threading.Lock()
        self.collateral_lock = threading.Lock()
        self.others_lock = threading.Lock()

        self.new_denoms_wfl_lock = threading.Lock()
        self.new_collateral_wfl_lock = threading.Lock()
        self.pay_collateral_wfl_lock = threading.Lock()
        self.denominate_wfl_lock = threading.Lock()

        # _ps_denoms_amount_cache recalculated in add_ps_denom/pop_ps_denom
        self._ps_denoms_amount_cache = 0
        denoms = wallet.db.get_ps_denoms()
        for addr, value, rounds in denoms.values():
            self._ps_denoms_amount_cache += value
        # _denoms_to_mix_cache recalculated on mix_rounds change and
        # in add[_mixing]_denom/pop[_mixing]_denom methods
        self._denoms_to_mix_cache = self.denoms_to_mix()

        # sycnhronizer unsubsribed addresses
        self.spent_addrs = set()
        self.unsubscribed_addrs = set()

        # postponed notification sent by trigger_postponed_notifications
        self.postponed_notifications = {}

    @property
    def unsupported(self):
        return self.state == PSStates.Unsupported

    @property
    def enabled(self):
        return self.state not in [PSStates.Unsupported, PSStates.Disabled]

    def enable_ps(self):
        if not self.enabled:
            self.wallet.db.set_ps_data('ps_enabled', True)
            coro = self._enable_ps()
            asyncio.run_coroutine_threadsafe(coro, self.loop)

    async def _enable_ps(self):
        if self.enabled:
            return
        self.state = PSStates.Initializing
        self.trigger_callback('ps-state-changes', self.wallet, None, None)
        _load_and_cleanup = self.load_and_cleanup
        await self.loop.run_in_executor(None, _load_and_cleanup)
        await self.find_untracked_ps_txs()

    @property
    def state(self):
        return self._state

    @state.setter
    def state(self, state):
        self._state = state

    def load_and_cleanup(self):
        if not self.enabled:
            return
        w = self.wallet
        # check last_mix_stop_time if it was not saved on wallet crash
        last_mix_start_time = self.last_mix_start_time
        last_mix_stop_time = self.last_mix_stop_time
        if last_mix_stop_time < last_mix_start_time:
            last_mixed_tx_time = self.last_mixed_tx_time
            wait_time = self.wait_for_mn_txs_time
            if last_mixed_tx_time > last_mix_start_time:
                self.last_mix_stop_time = last_mixed_tx_time + wait_time
            else:
                self.last_mix_stop_time = last_mix_start_time + wait_time
        # load and unsubscribe spent ps addresses
        unspent = w.db.get_unspent_ps_addresses()
        for addr in w.db.get_ps_addresses():
            if addr in unspent:
                continue
            self.spent_addrs.add(addr)
            if self.subscribe_spent:
                continue
            hist = w.db.get_addr_history(addr)
            self.unsubscribe_spent_addr(addr, hist)
        self._fix_uncompleted_ps_txs()

    def register_callback(self, callback, events):
        with self.callback_lock:
            for event in events:
                self.callbacks[event].append(callback)

    def unregister_callback(self, callback):
        with self.callback_lock:
            for callbacks in self.callbacks.values():
                if callback in callbacks:
                    callbacks.remove(callback)

    def trigger_callback(self, event, *args):
        try:
            with self.callback_lock:
                callbacks = self.callbacks[event][:]
            [callback(event, *args) for callback in callbacks]
        except Exception as e:
            self.logger.info(f'Error in trigger_callback: {str(e)}')

    def postpone_notification(self, event, *args):
        self.postponed_notifications[event] = args

    async def trigger_postponed_notifications(self):
        while True:
            await asyncio.sleep(0.5)
            for event in list(self.postponed_notifications.keys()):
                args = self.postponed_notifications.pop(event, None)
                if args is not None:
                    self.trigger_callback(event, *args)

    def on_network_start(self, network):
        self.network = network
        self.network.register_callback(self.on_wallet_updated,
                                       ['wallet_updated'])
        self.dash_net = network.dash_net
        self.loop = network.asyncio_loop
        self._loop_thread = network._loop_thread
        asyncio.ensure_future(self.clean_keypairs_on_timeout())
        asyncio.ensure_future(self.cleanup_staled_denominate_wfls())
        asyncio.ensure_future(self.trigger_postponed_notifications())

    def on_stop_threads(self):
        self.stop_mixing()
        self.network.unregister_callback(self.on_wallet_updated)

    async def on_wallet_updated(self, event, *args):
        if not self.enabled:
            return
        w = args[0]
        if w != self.wallet:
            return
        if w.is_up_to_date():
            if self.state in [PSStates.Initializing, PSStates.Ready]:
                await self.find_untracked_ps_txs()

    async def broadcast_transaction(self, tx, *, timeout=None) -> None:
        if self.enabled:
            w = self.wallet

            def check_spend_to_ps_addresses():
                for o in tx.outputs():
                    addr = o.address
                    if addr in w.db.get_ps_addresses():
                        msg = self.SPEND_TO_PS_ADDRS_MSG
                        raise PSSpendToPSAddressesError(msg)
            await self.loop.run_in_executor(None, check_spend_to_ps_addresses)

            def check_possible_dspend():
                with self.denoms_lock, self.collateral_lock:
                    warn = self.double_spend_warn
                    if not warn:
                        return
                    for txin in tx.inputs():
                        prev_h = txin['prevout_hash']
                        prev_n = txin['prevout_n']
                        outpoint = f'{prev_h}:{prev_n}'
                        if (w.db.get_ps_spending_collateral(outpoint)
                                or w.db.get_ps_spending_denom(outpoint)):
                            raise PSPossibleDoubleSpendError(warn)
            await self.loop.run_in_executor(None, check_possible_dspend)
        await self.network.broadcast_transaction(tx, timeout=timeout)

    @property
    def keep_amount(self):
        return self.wallet.db.get_ps_data('keep_amount', DEFAULT_KEEP_AMOUNT)

    @keep_amount.setter
    def keep_amount(self, amount):
        if self.state in self.mixing_running_states:
            return
        if self.keep_amount == amount:
            return
        amount = max(self.min_keep_amount, int(amount))
        amount = min(self.max_keep_amount, int(amount))
        self.wallet.db.set_ps_data('keep_amount', amount)

    @property
    def min_keep_amount(self):
        return MIN_KEEP_AMOUNT

    @property
    def max_keep_amount(self):
        return MAX_KEEP_AMOUNT

    def keep_amount_data(self, full_txt=False):
        if full_txt:
            return _('This amount acts as a threshold to turn off'
                     " PrivateSend mixing once it's reached.")
        else:
            return _('Amount of Dash to keep anonymized')

    @property
    def mix_rounds(self):
        return self.wallet.db.get_ps_data('mix_rounds', DEFAULT_MIX_ROUNDS)

    @mix_rounds.setter
    def mix_rounds(self, rounds):
        if self.state in self.mixing_running_states:
            return
        if self.mix_rounds == rounds:
            return
        rounds = max(self.min_mix_rounds, int(rounds))
        rounds = min(self.max_mix_rounds, int(rounds))
        self.wallet.db.set_ps_data('mix_rounds', rounds)
        with self.denoms_lock:
            self._denoms_to_mix_cache = self.denoms_to_mix()

    @property
    def min_mix_rounds(self):
        return MIN_MIX_ROUNDS

    @property
    def max_mix_rounds(self):
        if constants.net.TESTNET:
            return MAX_MIX_ROUNDS_TESTNET
        else:
            return MAX_MIX_ROUNDS

    def mix_rounds_data(self, full_txt=False):
        if full_txt:
            return _('This setting determines the amount of individual'
                     ' masternodes that a input will be anonymized through.'
                     ' More rounds of anonymization gives a higher degree'
                     ' of privacy, but also costs more in fees.')
        else:
            return _('PrivateSend rounds to use')

    @property
    def group_history(self):
        if self.unsupported:
            return False
        return self.wallet.db.get_ps_data('group_history',
                                          DEFAULT_GROUP_HISTORY)

    @group_history.setter
    def group_history(self, group_history):
        if self.group_history == group_history:
            return
        self.wallet.db.set_ps_data('group_history', bool(group_history))

    def group_history_data(self, full_txt=False):
        if full_txt:
            return _('Group PrivateSend mixing transactions in wallet history')
        else:
            return _('Group PrivateSend transactions')

    @property
    def notify_ps_txs(self):
        return self.wallet.db.get_ps_data('notify_ps_txs',
                                          DEFAULT_NOTIFY_PS_TXS)

    @notify_ps_txs.setter
    def notify_ps_txs(self, notify_ps_txs):
        if self.notify_ps_txs == notify_ps_txs:
            return
        self.wallet.db.set_ps_data('notify_ps_txs', bool(notify_ps_txs))

    def notify_ps_txs_data(self, full_txt=False):
        if full_txt:
            return _('Notify when PrivateSend mixing transactions is arrived')
        else:
            return _('Notify on PrivateSend transactions')

    def need_notify(self, txid):
        if self.notify_ps_txs:
            return True
        tx_type, completed = self.wallet.db.get_ps_tx(txid)
        if tx_type not in PS_MIXING_TX_TYPES:
            return True
        else:
            return False

    @property
    def max_sessions(self):
        return self.wallet.db.get_ps_data('max_sessions',
                                          DEFAULT_PRIVATESEND_SESSIONS)

    @max_sessions.setter
    def max_sessions(self, max_sessions):
        if self.max_sessions == max_sessions:
            return
        self.wallet.db.set_ps_data('max_sessions', int(max_sessions))

    @property
    def min_max_sessions(self):
        return MIN_PRIVATESEND_SESSIONS

    @property
    def max_max_sessions(self):
        return MAX_PRIVATESEND_SESSIONS

    def max_sessions_data(self, full_txt=False):
        if full_txt:
            return _('Count of PrivateSend mixing session')
        else:
            return _('PrivateSend sessions')

    @property
    def kp_timeout(self):
        return self.wallet.db.get_ps_data('kp_timeout', DEFAULT_KP_TIMEOUT)

    @kp_timeout.setter
    def kp_timeout(self, kp_timeout):
        if self.kp_timeout == kp_timeout:
            return
        kp_timeout = min(int(kp_timeout), MAX_KP_TIMEOUT)
        kp_timeout = max(kp_timeout, MIN_KP_TIMEOUT)
        self.wallet.db.set_ps_data('kp_timeout', kp_timeout)

    @property
    def min_kp_timeout(self):
        return MIN_KP_TIMEOUT

    @property
    def max_kp_timeout(self):
        return MAX_KP_TIMEOUT

    def kp_timeout_data(self, full_txt=False):
        if full_txt:
            return _('Time in minutes to keep keypairs after mixing stopped.'
                     ' Keypairs is cached before mixing starts on wallets with'
                     ' encrypted keystore.')
        else:
            return _('Keypairs cache timeout')

    @property
    def subscribe_spent(self):
        return self.wallet.db.get_ps_data('subscribe_spent',
                                          DEFAULT_SUBSCRIBE_SPENT)

    @subscribe_spent.setter
    def subscribe_spent(self, subscribe_spent):
        if self.subscribe_spent == subscribe_spent:
            return
        self.wallet.db.set_ps_data('subscribe_spent', bool(subscribe_spent))
        w = self.wallet
        if subscribe_spent:
            for addr in self.spent_addrs:
                self.subscribe_spent_addr(addr)
        else:
            for addr in self.spent_addrs:
                hist = w.db.get_addr_history(addr)
                self.unsubscribe_spent_addr(addr, hist)

    def subscribe_spent_data(self, full_txt=False):
        if full_txt:
            return _('Subscribe to spent PS addresses'
                     ' on electrum servers')
        else:
            return _('Subscribe to spent PS addresses')

    @property
    def ps_collateral_cnt(self):
        return len(self.wallet.db.get_ps_collaterals())

    def add_ps_spending_collateral(self, outpoint, wfl_uuid):
        self.wallet.db._add_ps_spending_collateral(outpoint, wfl_uuid)

    def pop_ps_spending_collateral(self, outpoint):
        return self.wallet.db._pop_ps_spending_collateral(outpoint)

    def add_ps_denom(self, outpoint, denom):  # denom is (addr, value, rounds)
        self.wallet.db._add_ps_denom(outpoint, denom)
        self._ps_denoms_amount_cache += denom[1]
        if denom[2] < self.mix_rounds:  # if rounds < mix_rounds
            self._denoms_to_mix_cache[outpoint] = denom

    def pop_ps_denom(self, outpoint):
        denom = self.wallet.db._pop_ps_denom(outpoint)
        if denom:
            self._ps_denoms_amount_cache -= denom[1]
            self._denoms_to_mix_cache.pop(outpoint, None)
        return denom

    def add_ps_spending_denom(self, outpoint, wfl_uuid):
        self.wallet.db._add_ps_spending_denom(outpoint, wfl_uuid)
        self._denoms_to_mix_cache.pop(outpoint, None)

    def pop_ps_spending_denom(self, outpoint):
        db = self.wallet.db
        denom = db.get_ps_denom(outpoint)
        if denom and denom[2] < self.mix_rounds:  # if rounds < mix_rounds
            self._denoms_to_mix_cache[outpoint] = denom
        return db._pop_ps_spending_denom(outpoint)

    @property
    def pay_collateral_wfl(self):
        d = self.wallet.db.get_ps_data('pay_collateral_wfl')
        if d:
            return PSTxWorkflow._from_dict(d)

    def set_pay_collateral_wfl(self, workflow):
        self.wallet.db.set_ps_data('pay_collateral_wfl', workflow._as_dict())
        self.postpone_notification('ps-wfl-changes', self.wallet)

    def clear_pay_collateral_wfl(self):
        self.wallet.db.set_ps_data('pay_collateral_wfl', {})
        self.postpone_notification('ps-wfl-changes', self.wallet)

    @property
    def new_collateral_wfl(self):
        d = self.wallet.db.get_ps_data('new_collateral_wfl')
        if d:
            return PSTxWorkflow._from_dict(d)

    def set_new_collateral_wfl(self, workflow):
        self.wallet.db.set_ps_data('new_collateral_wfl', workflow._as_dict())
        self.postpone_notification('ps-wfl-changes', self.wallet)

    def clear_new_collateral_wfl(self):
        self.wallet.db.set_ps_data('new_collateral_wfl', {})
        self.postpone_notification('ps-wfl-changes', self.wallet)

    @property
    def new_denoms_wfl(self):
        d = self.wallet.db.get_ps_data('new_denoms_wfl')
        if d:
            return PSTxWorkflow._from_dict(d)

    def set_new_denoms_wfl(self, workflow):
        self.wallet.db.set_ps_data('new_denoms_wfl', workflow._as_dict())
        self.postpone_notification('ps-wfl-changes', self.wallet)

    def clear_new_denoms_wfl(self):
        self.wallet.db.set_ps_data('new_denoms_wfl', {})
        self.postpone_notification('ps-wfl-changes', self.wallet)

    @property
    def denominate_wfl_list(self):
        wfls = self.wallet.db.get_ps_data('denominate_workflows', {})
        return list(wfls.keys())

    @property
    def active_denominate_wfl_cnt(self):
        cnt = 0
        for uuid in self.denominate_wfl_list:
            wfl = self.get_denominate_wfl(uuid)
            if wfl and not wfl.completed:
                cnt += 1
        return cnt

    def get_denominate_wfl(self, uuid):
        wfls = self.wallet.db.get_ps_data('denominate_workflows', {})
        wfl = wfls.get(uuid)
        if wfl:
            return PSDenominateWorkflow._from_uuid_and_tuple(uuid, wfl)

    def clear_denominate_wfl(self, uuid):
        self.wallet.db.pop_ps_data('denominate_workflows', uuid)
        self.postpone_notification('ps-wfl-changes', self.wallet)

    def set_denominate_wfl(self, workflow):
        wfl_dict = workflow._as_dict()
        self.wallet.db.update_ps_data('denominate_workflows', wfl_dict)
        self.postpone_notification('ps-wfl-changes', self.wallet)

    def mixing_control_data(self, full_txt=False):
        if full_txt:
            return _('Control PrivateSend mixing process')
        else:
            if self.state == PSStates.Ready:
                return _('Start Mixing')
            elif self.state == PSStates.Mixing:
                return _('Stop Mixing')
            elif self.state == PSStates.StartMixing:
                return _('Starting Mixing ...')
            elif self.state == PSStates.StopMixing:
                return _('Stopping Mixing ...')
            elif self.state == PSStates.FindingUntracked:
                return _('Finding PS Data ...')
            elif self.state == PSStates.Disabled:
                return _('Enable PrivateSend')
            elif self.state == PSStates.Initializing:
                return _('Initializing ...')
            else:
                return _('Check Log For Errors')

    @property
    def last_mix_start_time(self):
        return self.wallet.db.get_ps_data('last_mix_start_time', 0)  # Jan 1970

    @last_mix_start_time.setter
    def last_mix_start_time(self, time):
        self.wallet.db.set_ps_data('last_mix_start_time', time)

    @property
    def last_mix_stop_time(self):
        return self.wallet.db.get_ps_data('last_mix_stop_time', 0)  # Jan 1970

    @last_mix_stop_time.setter
    def last_mix_stop_time(self, time):
        self.wallet.db.set_ps_data('last_mix_stop_time', time)

    @property
    def last_mixed_tx_time(self):
        return self.wallet.db.get_ps_data('last_mixed_tx_time', 0)  # Jan 1970

    @last_mixed_tx_time.setter
    def last_mixed_tx_time(self, time):
        self.wallet.db.set_ps_data('last_mixed_tx_time', time)

    @property
    def wait_for_mn_txs_time(self):
        return WAIT_FOR_MN_TXS_TIME_SEC

    @property
    def mix_stop_secs_ago(self):
        return round(time.time() - self.last_mix_stop_time)

    @property
    def mix_recently_run(self):
        return self.mix_stop_secs_ago < self.wait_for_mn_txs_time

    @property
    def double_spend_warn(self):
        if self.state in self.mixing_running_states:
            wait_time = self.wait_for_mn_txs_time
            return _('PrivateSend mixing is currently run. To prevent'
                     ' double spending it is recommended to stop mixing'
                     ' and wait {} seconds before spending PrivateSend'
                     ' coins.'.format(wait_time))
        if self.mix_recently_run:
            wait_secs = self.wait_for_mn_txs_time - self.mix_stop_secs_ago
            if wait_secs > 0:
                return _('PrivateSend mixing is recently run. To prevent'
                         ' double spending It is recommended to wait'
                         ' {} seconds before spending PrivateSend'
                         ' coins.'.format(wait_secs))
        return ''

    def dn_balance_data(self, full_txt=False):
        if full_txt:
            return _('Currently available denominated balance')
        else:
            return _('Denominated Balance')

    def ps_balance_data(self, full_txt=False):
        if full_txt:
            return _('Currently available anonymized balance')
        else:
            return _('PrivateSend Balance')

    @property
    def show_warn_electrumx(self):
        return self.wallet.db.get_ps_data('show_warn_electrumx', True)

    @show_warn_electrumx.setter
    def show_warn_electrumx(self, show):
        self.wallet.db.set_ps_data('show_warn_electrumx', show)

    def warn_electrumx_data(self, full_txt=False, help_txt=False):
        if full_txt:
            return _('Privacy Warning: ElectrumX is a weak spot'
                     ' in PrivateSend privacy and knows all your'
                     ' wallet UTXO including PrivateSend mixed denoms.'
                     ' You should use trusted ElectrumX server'
                     ' for PrivateSend operation.')
        elif help_txt:
            return _('Show privacy warning about ElectrumX serves usage')
        else:
            return _('Privacy Warning ...')

    def get_ps_data_info(self):
        res = []
        w = self.wallet
        data = w.db.get_ps_txs()
        res.append(f'PrivateSend transactions count: {len(data)}')
        data = w.db.get_ps_txs_removed()
        res.append(f'Removed PrivateSend transactions count: {len(data)}')

        data = w.db.get_ps_denoms()
        res.append(f'ps_denoms count: {len(data)}')
        data = w.db.get_ps_spent_denoms()
        res.append(f'ps_spent_denoms count: {len(data)}')
        data = w.db.get_ps_spending_denoms()
        res.append(f'ps_spending_denoms count: {len(data)}')

        data = w.db.get_ps_collaterals()
        res.append(f'ps_collaterals count: {len(data)}')
        data = w.db.get_ps_spent_collaterals()
        res.append(f'ps_spent_collaterals count: {len(data)}')
        data = w.db.get_ps_spending_collaterals()
        res.append(f'ps_spending_collaterals count: {len(data)}')

        data = w.db.get_ps_others()
        res.append(f'ps_others count: {len(data)}')
        data = w.db.get_ps_spent_others()
        res.append(f'ps_spent_others count: {len(data)}')

        data = w.db.get_ps_reserved()
        res.append(f'Reserved addresses count: {len(data)}')

        if self.pay_collateral_wfl:
            res.append(f'Pay collateral workflow data exists')

        if self.new_collateral_wfl:
            res.append(f'New collateral workflow data exists')

        if self.new_denoms_wfl:
            res.append(f'New denoms workflow data exists')

        dwfl_cnt = 0
        completed_dwfl_cnt = 0
        dwfl_list = self.denominate_wfl_list
        dwfl_cnt = len(dwfl_list)
        for uuid in dwfl_list:
            wfl = self.get_denominate_wfl(uuid)
            if wfl and wfl.completed:
                completed_dwfl_cnt += 1
        if dwfl_cnt:
            res.append(f'Denominate workflow count: {dwfl_cnt},'
                       f' completed: {completed_dwfl_cnt}')

        if self._keypairs_cache:
            for cache_type in KP_ALL_TYPES:
                if cache_type in self._keypairs_cache:
                    cnt = len(self._keypairs_cache[cache_type])
                    res.append(f'Keypairs cache type: {cache_type}'
                               f' cached keys: {cnt}')
        return res

    def mixing_progress(self, count_on_rounds=None):
        w = self.wallet
        dn_balance = sum(w.get_balance(include_ps=False, min_rounds=0))
        if dn_balance == 0:
            return 0
        dn_balance = sum(w.get_balance(include_ps=False, min_rounds=0))
        r = self.mix_rounds if count_on_rounds is None else count_on_rounds
        ps_balance = sum(w.get_balance(include_ps=False, min_rounds=r))
        if dn_balance == ps_balance:
            return 100
        res = 0
        for i in range(1, r+1):
            ri_balance = sum(w.get_balance(include_ps=False, min_rounds=i))
            res += ri_balance/dn_balance/r
        res = round(res*100)
        if res < 100:  # on small amount differences show 100 percents to early
            return res
        else:
            return 99

    def mixing_progress_data(self, full_txt=False):
        if full_txt:
            return _('Mixing Progress in percents')
        else:
            return _('Mixing Progress')

    @property
    def all_mixed(self):
        w = self.wallet
        dn_balance = sum(w.get_balance(include_ps=False, min_rounds=0))
        if dn_balance == 0:
            return False
        r = self.mix_rounds
        ps_balance = sum(w.get_balance(include_ps=False, min_rounds=r))
        return (dn_balance and ps_balance >= dn_balance)

    # Methods related to keypairs cache
    def on_wallet_password_set(self):
        self.stop_mixing(self.WALLET_PASSWORD_SET_MSG)

    async def clean_keypairs_on_timeout(self):
        while True:
            def _clean_keypairs():
                if not self._keypairs_cache:
                    return
                if self.mix_stop_secs_ago < self.kp_timeout:
                    return
                with self.state_lock, self.keypairs_state_lock:
                    if self.state in self.mixing_running_states:
                        return
                    if self._keypairs_state != KPStates.AllDone:
                        return
                    self._keypairs_state = KPStates.Cleaning
                    self.logger.info('Cleaning Keyparis Cache'
                                     ' on inactivity timeout')
                    self._cleanup_all_keypairs_cache()
                    self.logger.info('Cleaned Keyparis Cache')
                    self._keypairs_state = KPStates.Empty
            await self.loop.run_in_executor(None, _clean_keypairs)
            await asyncio.sleep(10)

    async def _make_keypairs_cache(self, password):
        if password is None:
            return
        while True:
            if self._keypairs_state == KPStates.AllDone:
                return
            if self._keypairs_state not in [KPStates.NeedGen, KPStates.Empty]:
                await asyncio.sleep(1)
                continue
            with self.keypairs_state_lock:
                if self._keypairs_state in [KPStates.NeedGen, KPStates.Empty]:
                    self._keypairs_state = KPStates.Generating
            if self._keypairs_state == KPStates.Generating:
                _make_cache = self._cache_keypairs
                await self.loop.run_in_executor(None, _make_cache, password)
                break

    def calc_need_new_keypairs_cnt(self):
        w = self.wallet
        old_denoms_cnt = len(w.db.get_ps_denoms(min_rounds=0))
        old_denoms_amnt = sum(self.wallet.get_balance(include_ps=False,
                                                      min_rounds=0))
        # check if new denoms tx will be created
        keep_value = to_duffs(self.keep_amount)
        need_amnt = keep_value - old_denoms_amnt + COLLATERAL_VAL
        new_denoms_approx = self.find_denoms_approx(need_amnt)
        new_denoms_cnt = sum([len(a) for a in new_denoms_approx])

        # calc need sign denoms for each round
        total_denoms_cnt = old_denoms_cnt + new_denoms_cnt
        sign_denoms_cnt = 0
        for r in range(self.mix_rounds, 0, -1):
            # for round 0 used keypairs from regular spendable coins
            rn_denoms_cnt = len(w.db.get_ps_denoms(min_rounds=r))
            sign_denoms_cnt += (total_denoms_cnt - rn_denoms_cnt)

        # Dash Core charges the collateral randomly in 1/10 mixing transactions
        # * avg denoms in mixing transactions is 5 (1-9), but real count
        #   currently is about ~1.1 as suitable denoms is filtered
        pay_collateral_cnt = ceil(sign_denoms_cnt/10/1.1)
        # * pay collateral uses change in 3/4 of cases (1/4 OP_RETURN output)
        need_sign_change_cnt = ceil(pay_collateral_cnt*0.75)
        # * new collateral has value for 4 pay collateral txs
        new_collateral_cnt = ceil(pay_collateral_cnt*0.25)

        need_sign_cnt = sign_denoms_cnt + new_collateral_cnt
        return need_sign_cnt, need_sign_change_cnt

    def check_need_new_keypairs(self):
        w = self.wallet
        if not w.has_password():
            return False

        with self.keypairs_state_lock:
            if self._keypairs_state in [KPStates.Cleaning, KPStates.Empty]:
                return True
            elif self._keypairs_state != KPStates.AllDone:
                return False

            for cache_type in KP_ALL_TYPES:
                if cache_type not in self._keypairs_cache:
                    self._keypairs_state = KPStates.NeedGen
                    return True

            # check spendable regular coins keys
            for c in w.get_utxos(None):
                if c['address'] not in self._keypairs_cache[KP_SPENDABLE]:
                    self._keypairs_state = KPStates.NeedGen
                    return True

            # check spendable ps coins keys (already saved denoms/collateral)
            for c in w.get_utxos(None, min_rounds=PSCoinRounds.COLLATERAL):
                if c['address'] not in self._keypairs_cache[KP_PS_SPENDABLE]:
                    self._keypairs_state = KPStates.NeedGen
                    return True

            # check new denoms/collateral signing keys to future coins
            sign_cnt, sign_change_cnt = self.calc_need_new_keypairs_cnt()
            if sign_cnt - len(self._keypairs_cache[KP_PS_COINS]) > 0:
                self._keypairs_state = KPStates.NeedGen
                return True
            if sign_change_cnt - len(self._keypairs_cache[KP_PS_CHANGE]) > 0:
                self._keypairs_state = KPStates.NeedGen
                return True
            return False

    def _cache_keypairs(self, password):
        w = self.wallet
        self.logger.info('Making Keyparis Cache')
        for cache_type in KP_ALL_TYPES:
            if cache_type not in self._keypairs_cache:
                self._keypairs_cache[cache_type] = {}

        # add spendable regular coins keys
        cached = 0
        for c in w.get_utxos(None):
            addr = c['address']
            if addr in self._keypairs_cache[KP_SPENDABLE]:
                continue
            sequence = w.get_address_index(addr)
            x_pubkey = w.keystore.get_xpubkey(*sequence)
            sec = w.keystore.get_private_key(sequence, password)
            self._keypairs_cache[KP_SPENDABLE][addr] = (x_pubkey, sec)
            cached += 1
        if cached:
            self.logger.info(f'Cached {cached} keys of {KP_SPENDABLE} type')
        self._keypairs_state = KPStates.SpendableDone

        # add spendable ps coins keys (already presented denoms/collateral)
        cached = 0
        for c in w.get_utxos(None, min_rounds=PSCoinRounds.COLLATERAL):
            addr = c['address']
            if addr in self._keypairs_cache[KP_PS_SPENDABLE]:
                continue
            prev_h = c['prevout_hash']
            prev_n = c['prevout_n']
            outpoint = f'{prev_h}:{prev_n}'
            ps_denom = w.db.get_ps_denom(outpoint)
            if ps_denom and ps_denom[2] >= self.mix_rounds:
                continue
            sequence = w.get_address_index(addr)
            x_pubkey = w.keystore.get_xpubkey(*sequence)
            sec = w.keystore.get_private_key(sequence, password)
            self._keypairs_cache[KP_PS_SPENDABLE][addr] = (x_pubkey, sec)
            cached += 1
        if cached:
            self.logger.info(f'Cached {cached} keys of {KP_PS_SPENDABLE} type')
        self._keypairs_state = KPStates.PSSpendableDone

        # add new denoms/collateral signing keys to future coins
        sign_cnt, sign_change_cnt = self.calc_need_new_keypairs_cnt()
        sign_cnt -= len(self._keypairs_cache[KP_PS_COINS])
        sign_change_cnt -= len(self._keypairs_cache[KP_PS_CHANGE])

        ps_spendable_addrs = list(self._keypairs_cache[KP_PS_SPENDABLE].keys())
        ps_change_addrs = list(self._keypairs_cache[KP_PS_CHANGE].keys())
        ps_coins_addrs = list(self._keypairs_cache[KP_PS_COINS].keys())

        # add keys for ps_reserved addresses
        for addr, data in self.wallet.db.get_ps_reserved().items():
            if w.is_change(addr):
                if addr in ps_change_addrs:
                    continue
                sequence = w.get_address_index(addr)
                x_pubkey = w.keystore.get_xpubkey(*sequence)
                sec = w.keystore.get_private_key(sequence, password)
                self._keypairs_cache[KP_PS_CHANGE][addr] = (x_pubkey, sec)
                sign_change_cnt -= 1
            else:
                if addr in ps_coins_addrs:
                    continue
                sequence = w.get_address_index(addr)
                x_pubkey = w.keystore.get_xpubkey(*sequence)
                sec = w.keystore.get_private_key(sequence, password)
                self._keypairs_cache[KP_PS_COINS][addr] = (x_pubkey, sec)
                sign_cnt -= 1
        # add keys for unused addresses
        if sign_change_cnt > 0:
            first_change_index = self.first_unused_index(for_change=True)
            cached = 0
            ci = first_change_index
            while cached < sign_change_cnt:
                sequence = [1, ci]
                x_pubkey = w.keystore.get_xpubkey(*sequence)
                _, addr = xpubkey_to_address(x_pubkey)
                if addr in ps_spendable_addrs or addr in ps_change_addrs:
                    ci += 1
                    continue
                sec = w.keystore.get_private_key(sequence, password)
                self._keypairs_cache[KP_PS_CHANGE][addr] = (x_pubkey, sec)
                ci += 1
                cached += 1
                if not cached % 100:
                    self.logger.info(f'Cached {cached} keys'
                                     f' of {KP_PS_CHANGE} type')
            if cached:
                self.logger.info(f'Cached {cached} keys'
                                 f' of {KP_PS_CHANGE} type')
        self._keypairs_state = KPStates.PSChangeDone

        if sign_cnt > 0:
            first_recv_index = self.first_unused_index(for_change=False)
            cached = 0
            ri = first_recv_index
            while cached < sign_cnt:
                sequence = [0, ri]
                x_pubkey = w.keystore.get_xpubkey(*sequence)
                _, addr = xpubkey_to_address(x_pubkey)
                if addr in ps_spendable_addrs or addr in ps_coins_addrs:
                    ri += 1
                    continue
                sec = w.keystore.get_private_key(sequence, password)
                self._keypairs_cache[KP_PS_COINS][addr] = (x_pubkey, sec)
                cached += 1
                ri += 1
                if not cached % 100:
                    self.logger.info(f'Cached {cached} keys'
                                     f' of {KP_PS_COINS} type')
            if cached:
                self.logger.info(f'Cached {cached} keys'
                                 f' of {KP_PS_COINS} type')
        self._keypairs_state = KPStates.AllDone
        self.logger.info('Keyparis Cache Done')

    def _find_addrs_not_in_keypairs(self, addrs):
        addrs = set(addrs)
        found = set()
        for cache_type in KP_ALL_TYPES:
            if cache_type in self._keypairs_cache:
                cache = self._keypairs_cache[cache_type]
                for c_addrs in cache.keys():
                    for addr in addrs:
                        if addr in found:
                            continue
                        if addr in c_addrs:
                            found.add(addr)
        return addrs - found

    def unpack_mine_input_addrs(func):
        '''Decorator to prepare tx inputs addresses'''
        def func_wrapper(self, txid, tx, tx_type):
            w = self.wallet
            inputs = []
            for i in tx.inputs():
                prev_h = i['prevout_hash']
                prev_n = i['prevout_n']
                outpoint = f'{prev_h}:{prev_n}'
                prev_tx = w.db.get_transaction(prev_h)
                if prev_tx:
                    o = prev_tx.outputs()[prev_n]
                    if w.is_mine(o.address):
                        inputs.append((outpoint, o.address))
            return func(self, txid, tx_type, inputs, tx.outputs())
        return func_wrapper

    @unpack_mine_input_addrs
    def _cleanup_spendable_keypairs(self, txid, tx_type, inputs, outputs):
        spendable_cache = self._keypairs_cache.get(KP_SPENDABLE, {})
        last_output_addr = outputs[-1].address
        # cleanup spendable keypairs
        for outpoint, addr in inputs:
            if addr != last_output_addr and addr in spendable_cache:
                spendable_cache.pop(addr)

        # move ps coins keypairs to ps spendable cache
        ps_coins_cache = self._keypairs_cache.get(KP_PS_COINS, {})
        ps_spendable_cache = self._keypairs_cache.get(KP_PS_SPENDABLE, {})
        for o in outputs:
            addr = o.address
            if addr in ps_coins_cache:
                keypair = ps_coins_cache.pop(addr, None)
                if keypair is not None:
                    ps_spendable_cache[addr] = keypair

    @unpack_mine_input_addrs
    def _cleanup_ps_keypairs(self, txid, tx_type, inputs, outputs):
        ps_spendable_cache = self._keypairs_cache.get(KP_PS_SPENDABLE, {})
        ps_coins_cache = self._keypairs_cache.get(KP_PS_COINS, {})
        ps_change_cache = self._keypairs_cache.get(KP_PS_CHANGE, {})

        # cleanup ps spendable keypairs
        for outpoint, addr in inputs:
            if addr in ps_spendable_cache:
                ps_spendable_cache.pop(addr)

        # move ps change, ps coins keypairs to ps spendable cache
        w = self.wallet
        for i, o in enumerate(outputs):
            addr = o.address
            if addr in ps_change_cache:
                keypair = ps_change_cache.pop(addr, None)
                if keypair is not None and tx_type == PSTxTypes.PAY_COLLATERAL:
                    ps_spendable_cache[addr] = keypair
            elif addr in ps_coins_cache:
                keypair = ps_coins_cache.pop(addr, None)
                if keypair is not None and tx_type == PSTxTypes.DENOMINATE:
                    outpoint = f'{txid}:{i}'
                    ps_denom = w.db.get_ps_denom(outpoint)
                    if ps_denom and ps_denom[2] < self.mix_rounds:
                        ps_spendable_cache[addr] = keypair

    @property
    def is_enough_ps_coins_cache(self):
        cache_type = KP_PS_COINS
        if cache_type not in self._keypairs_cache:
            return False
        if self._keypairs_state == KPStates.AllDone:
            return True
        elif len(self._keypairs_cache[cache_type].keys()) >= 100:
            return True
        else:
            return False

    def _cleanup_all_keypairs_cache(self):
        if not self._keypairs_cache:
            return False
        for cache_type in KP_ALL_TYPES:
            if cache_type not in self._keypairs_cache:
                continue
            for addr in list(self._keypairs_cache[cache_type].keys()):
                self._keypairs_cache[cache_type].pop(addr)
            self._keypairs_cache.pop(cache_type)
        return True

    def get_keypairs(self):
        keypairs = {}
        for cache_type in KP_ALL_TYPES:
            if cache_type not in self._keypairs_cache:
                continue
            for x_pubkey, sec in self._keypairs_cache[cache_type].values():
                keypairs[x_pubkey] = sec
        return keypairs

    def sign_transaction(self, tx, password, mine_txins_cnt=None):
        if self._keypairs_cache:
            if mine_txins_cnt is None:
                tx.add_inputs_info(self.wallet)
            keypairs = self.get_keypairs()
            signed_txins_cnt = tx.sign(keypairs)
            keypairs.clear()
            if mine_txins_cnt is None:
                mine_txins_cnt = len(tx.inputs())
            if signed_txins_cnt < mine_txins_cnt:
                self.logger.debug(f'mine txins cnt: {mine_txins_cnt},'
                                  f' signed txins cnt: {signed_txins_cnt}')
                raise SignWithKeypairsFailed('Tx signing failed')
        else:
            self.wallet.sign_transaction(tx, password)
        return tx

    # Methods related to mixing process
    def check_protx_info_completeness(self):
        if not self.network:
            return False
        mn_list = self.network.mn_list
        if mn_list.protx_info_completeness < 0.75:
            return False
        else:
            return True

    def check_llmq_ready(self):
        if not self.network:
            return False
        mn_list = self.network.mn_list
        return mn_list.llmq_ready

    def start_mixing(self, password):
        w = self.wallet
        msg = None
        if self.all_mixed and not self.calc_need_denoms_amounts():
            msg = self.ALL_MIXED_MSG, 'inf'
        elif not self.network or not self.network.is_connected():
            msg = self.NO_NETWORK_MSG, 'err'
        elif not self.dash_net.run_dash_net:
            msg = self.NO_DASH_NET_MSG, 'err'
        #elif not self.check_llmq_ready():
        #    msg = self.LLMQ_DATA_NOT_READY, 'err'
        #elif not self.check_protx_info_completeness():
        #    msg = self.MNS_DATA_NOT_READY, 'err'
        if msg:
            msg, inf = msg
            self.logger.info(f'Can not start PrivateSend Mixing: {msg}')
            self.trigger_callback('ps-state-changes', w, msg, inf)
            return

        coro = self.find_untracked_ps_txs()
        asyncio.run_coroutine_threadsafe(coro, self.loop).result()

        with self.state_lock:
            if self.state == PSStates.Ready:
                self.state = PSStates.StartMixing
            elif self.state in [PSStates.Unsupported, PSStates.Disabled]:
                msg = self.NOT_ENABLED_MSG
            elif self.state == PSStates.Initializing:
                msg = self.INITIALIZING_MSG
            elif self.state in self.mixing_running_states:
                msg = self.ALREADY_RUN_MSG
            elif self.state == PSStates.FindingUntracked:
                msg = self.FIND_UNTRACKED_RUN_MSG
            elif self.state == PSStates.FindingUntracked:
                msg = self.ERRORED_MSG
            else:
                msg = self.UNKNOWN_STATE_MSG.format(self.state)
        if msg:
            self.trigger_callback('ps-state-changes', w, msg, None)
            self.logger.info(f'Can not start PrivateSend Mixing: {msg}')
            return
        else:
            self.trigger_callback('ps-state-changes', w, None, None)

        fut = asyncio.run_coroutine_threadsafe(self._start_mixing(password),
                                               self.loop)
        try:
            fut.result(timeout=2)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass

        with self.state_lock:
            self.state = PSStates.Mixing
        self.last_mix_start_time = time.time()
        self.trigger_callback('ps-state-changes', w, None, None)

    async def _start_mixing(self, password):
        if not self.enabled or not self.network:
            return

        assert not self.main_taskgroup
        self.main_taskgroup = main_taskgroup = SilentTaskGroup()
        self.logger.info('Starting PrivateSend Mixing')

        async def main():
            try:
                async with main_taskgroup as group:
                    await group.spawn(self._make_keypairs_cache(password))
                    await group.spawn(self._check_all_mixed())
                    await group.spawn(self._maintain_pay_collateral_tx())
                    await group.spawn(self._maintain_collateral_amount())
                    await group.spawn(self._maintain_denoms())
                    await group.spawn(self._mix_denoms())
            except Exception as e:
                self.logger.exception('')
                raise e
        asyncio.run_coroutine_threadsafe(main(), self.loop)
        self.logger.info('Started PrivateSend Mixing')

    async def stop_mixing_from_async_thread(self, msg, msg_type=None):
        await self.loop.run_in_executor(None, self.stop_mixing, msg, msg_type)

    def stop_mixing(self, msg=None, msg_type=None):
        w = self.wallet
        with self.state_lock:
            if self.state != PSStates.Mixing:
                return
            self.state = PSStates.StopMixing
        self.trigger_callback('ps-state-changes', w, None, None)

        if msg:
            self.logger.info(f'Stopping PrivateSend Mixing: {msg}')
        else:
            self.logger.info('Stopping PrivateSend Mixing')
        fut = asyncio.run_coroutine_threadsafe(self._stop_mixing(), self.loop)
        self.logger.info('Stopped PrivateSend Mixing')
        try:
            fut.result(timeout=2)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass

        self.last_mix_stop_time = time.time()
        with self.state_lock:
            self.state = PSStates.Ready
        if msg is not None:
            if not msg_type or not msg_type.startswith('inf'):
                stopped_prefix = _('PrivateSend mixing is stoppped!')
                msg = f'{stopped_prefix}\n\n{msg}'
        self.trigger_callback('ps-state-changes', w, msg, msg_type)

    @log_exceptions
    async def _stop_mixing(self):
        if not self.main_taskgroup:
            return
        try:
            await asyncio.wait_for(self.main_taskgroup.cancel_remaining(),
                                   timeout=2)
        except (asyncio.TimeoutError, asyncio.CancelledError) as e:
            self.logger.debug(f'Exception during main_taskgroup cancellation: '
                              f'{repr(e)}')
        self.main_taskgroup = None

    async def _check_all_mixed(self):
        while not self.main_taskgroup.closed():
            await asyncio.sleep(10)
            if self.all_mixed:
                await self.stop_mixing_from_async_thread(self.ALL_MIXED_MSG,
                                                         'inf')

    async def _maintain_pay_collateral_tx(self):
        if self.wallet.has_password():
            kp_wait_states = [KPStates.Empty, KPStates.NeedGen,
                              KPStates.Generating, KPStates.SpendableDone,
                              KPStates.PSSpendableDone]
        else:
            kp_wait_states = []

        while not self.main_taskgroup.closed():
            wfl = self.pay_collateral_wfl
            if wfl:
                if not wfl.completed or not wfl.tx_order:
                    await self.cleanup_pay_collateral_wfl()
            elif self.ps_collateral_cnt > 0:
                if kp_wait_states and self._keypairs_state in kp_wait_states:
                    self.logger.info(f'Pay collateral workflow waiting'
                                     f' for keypairs generation')
                    await asyncio.sleep(5)
                    continue
                if not self.get_confirmed_ps_collateral_data():
                    await asyncio.sleep(5)
                    continue
                await self.prepare_pay_collateral_wfl()
            await asyncio.sleep(0.25)

    async def _maintain_collateral_amount(self):
        if self.wallet.has_password():
            kp_wait_states = [KPStates.Empty, KPStates.NeedGen,
                              KPStates.Generating]
        else:
            kp_wait_states = []

        while not self.main_taskgroup.closed():
            wfl = self.new_collateral_wfl
            if wfl:
                if not wfl.completed or not wfl.tx_order:
                    await self.cleanup_new_collateral_wfl()
                elif wfl.completed and wfl.next_to_send(self.wallet):
                    await self.broadcast_new_collateral_wfl()
            elif (not self.ps_collateral_cnt
                    and not self.calc_need_denoms_amounts(use_cache=True)):
                if kp_wait_states and self._keypairs_state in kp_wait_states:
                    self.logger.info(f'New collateral workflow waiting'
                                     f' for keypairs generation')
                    await asyncio.sleep(5)
                    continue
                await self.create_new_collateral_wfl()
            await asyncio.sleep(0.25)

    async def _maintain_denoms(self):
        if self.wallet.has_password():
            kp_wait_states = [KPStates.Empty, KPStates.NeedGen,
                              KPStates.Generating]
        else:
            kp_wait_states = []

        while not self.main_taskgroup.closed():
            wfl = self.new_denoms_wfl
            if wfl:
                if not wfl.completed or not wfl.tx_order:
                    await self.cleanup_new_denoms_wfl()
                elif wfl.completed and wfl.next_to_send(self.wallet):
                    await self.broadcast_new_denoms_wfl()
            elif self.calc_need_denoms_amounts(use_cache=True):
                if kp_wait_states and self._keypairs_state in kp_wait_states:
                    self.logger.info(f'New denoms workflow waiting'
                                     f' for keypairs generation')
                    await asyncio.sleep(5)
                    continue
                await self.create_new_denoms_wfl()
            await asyncio.sleep(0.25)

    async def _mix_denoms(self):
        if self.wallet.has_password():
            kp_wait_states = [KPStates.Empty, KPStates.NeedGen,
                              KPStates.Generating, KPStates.SpendableDone,
                              KPStates.PSSpendableDone]
        else:
            kp_wait_states = []

        def _cleanup():
            for uuid in self.denominate_wfl_list:
                wfl = self.get_denominate_wfl(uuid)
                if wfl and not wfl.completed:
                    self._cleanup_denominate_wfl(wfl)
        await self.loop.run_in_executor(None, _cleanup)

        while not self.main_taskgroup.closed():
            if (self._denoms_to_mix_cache
                    and self.pay_collateral_wfl
                    and self.active_denominate_wfl_cnt < self.max_sessions):
                if (kp_wait_states and
                        (self._keypairs_state in kp_wait_states
                         or not self.is_enough_ps_coins_cache)):
                    self.logger.info(f'Denominate workflow waiting'
                                     f' for keypairs generation')
                    await asyncio.sleep(5)
                    continue
                await self.main_taskgroup.spawn(self.start_denominate_wfl())
            await asyncio.sleep(0.25)

    def check_min_rounds(self, coins, min_rounds):
        for c in coins:
            ps_rounds = c['ps_rounds']
            if ps_rounds is None or ps_rounds < min_rounds:
                raise PSMinRoundsCheckFailed(f'Check for mininum {min_rounds}'
                                             f' PrivateSend mixing rounds'
                                             f' failed')

    async def start_mix_session(self, denom_value, dsq, wfl_lid):
        n_denom = PS_DENOMS_DICT[denom_value]
        sess = PSMixSession(self, denom_value, n_denom, dsq, wfl_lid)
        peer_str = sess.peer_str
        async with self.mix_sessions_lock:
            if peer_str in self.mix_sessions:
                raise Exception(f'Session with {peer_str} already exists')
            await sess.run_peer()
            self.mix_sessions[peer_str] = sess
            return sess

    async def stop_mix_session(self, peer_str):
        async with self.mix_sessions_lock:
            sess = self.mix_sessions.pop(peer_str)
            if not sess:
                self.logger.debug(f'Peer {peer_str} not found in mix_session')
                return
            sess.close_peer()
            return sess

    def get_addresses(self, include_ps=False, min_rounds=None,
                      for_change=None):
        if for_change is None:
            all_addrs = self.wallet.get_addresses()
        elif for_change:
            all_addrs = self.wallet.get_change_addresses()
        else:
            all_addrs = self.wallet.get_receiving_addresses()
        if include_ps:
            return all_addrs
        else:
            ps_addrs = self.wallet.db.get_ps_addresses(min_rounds=min_rounds)
        if min_rounds is not None:
            return [addr for addr in all_addrs if addr in ps_addrs]
        else:
            return [addr for addr in all_addrs if addr not in ps_addrs]

    def reserve_addresses(self, addrs_count, for_change=False, data=None):
        result = []
        w = self.wallet
        with w.lock:
            while len(result) < addrs_count:
                if for_change:
                    unused = w.calc_unused_change_addresses()
                else:
                    unused = w.get_unused_addresses()
                if unused:
                    addr = unused[0]
                else:
                    addr = w.create_new_address(for_change)
                w.db.add_ps_reserved(addr, data)
                result.append(addr)
        return result

    def first_unused_index(self, for_change=False):
        w = self.wallet
        with w.lock:
            if for_change:
                unused = w.calc_unused_change_addresses()
            else:
                unused = w.get_unused_addresses()
            if unused:
                return w.get_address_index(unused[0])[1]
            # no unused, return first index beyond last address in db
            if for_change:
                return w.db.num_change_addresses()
            else:
                return w.db.num_receiving_addresses()

    def add_spent_addrs(self, addrs):
        w = self.wallet
        unspent = w.db.get_unspent_ps_addresses()
        for addr in addrs:
            if addr in unspent:
                continue
            self.spent_addrs.add(addr)

    def restore_spent_addrs(self, addrs):
        for addr in addrs:
            self.spent_addrs.remove(addr)
            self.subscribe_spent_addr(addr)

    def subscribe_spent_addr(self, addr):
        w = self.wallet
        if addr in self.unsubscribed_addrs:
            self.unsubscribed_addrs.remove(addr)
            if w.synchronizer:
                self.logger.debug(f'Add {addr} to synchronizer')
                w.synchronizer.add(addr)

    def unsubscribe_spent_addr(self, addr, hist):
        if (self.subscribe_spent
                or addr not in self.spent_addrs
                or addr in self.unsubscribed_addrs
                or not hist):
            return
        w = self.wallet
        local_height = w.get_local_height()
        for hist_item in hist:
            txid = hist_item[0]
            verified_tx = w.db.verified_tx.get(txid)
            if not verified_tx:
                return
            height = verified_tx[0]
            conf = local_height - height + 1
            if conf < 6:
                return
        self.unsubscribed_addrs.add(addr)
        if w.synchronizer:
            self.logger.debug(f'Remove {addr} from synchronizer')
            w.synchronizer.remove_addr(addr)

    def calc_need_denoms_amounts(self, coins=None, use_cache=False):
        if use_cache:
            denoms_amount = self._ps_denoms_amount_cache
        else:
            denoms_amount = sum(self.wallet.get_balance(include_ps=False,
                                                        min_rounds=0))
        if coins is not None:
            coins_amount = 0
            for c in coins:
                coins_amount += c['value']
            max_keep_amount_duffs = to_duffs(self.max_keep_amount)
            if coins_amount + denoms_amount > max_keep_amount_duffs:
                need_amount = max_keep_amount_duffs
            else:
                need_amount = coins_amount
                # room for fees
                need_amount -= PS_DENOMS_VALS[0] + COLLATERAL_VAL + 1
        else:
            keep_amount_duffs = to_duffs(self.keep_amount)
            need_amount = keep_amount_duffs - denoms_amount
            need_amount += COLLATERAL_VAL  # room for fees
        return self.find_denoms_approx(need_amount)

    def find_denoms_approx(self, need_amount):
        if need_amount < COLLATERAL_VAL:
            return []

        denoms_amounts = []
        denoms_total = 0
        approx_found = False

        while not approx_found:
            cur_approx_amounts = []

            for dval in PS_DENOMS_VALS:
                for dn in range(11):  # max 11 values of same denom
                    if denoms_total + dval > need_amount:
                        if dval == PS_DENOMS_VALS[0]:
                            approx_found = True
                            denoms_total += dval
                            cur_approx_amounts.append(dval)
                        break
                    else:
                        denoms_total += dval
                        cur_approx_amounts.append(dval)
                if approx_found:
                    break

            denoms_amounts.append(cur_approx_amounts)
        return denoms_amounts

    def denoms_to_mix(self, mix_rounds=None, denom_value=None):
        res = {}
        w = self.wallet
        if mix_rounds is not None:
            denoms = w.db.get_ps_denoms(min_rounds=mix_rounds,
                                        max_rounds=mix_rounds)
        else:
            denoms = w.db.get_ps_denoms(max_rounds=self.mix_rounds-1)
        for outpoint, denom in denoms.items():
            if denom_value is not None and denom_value != denom[1]:
                continue
            if not w.db.get_ps_spending_denom(outpoint):
                res.update({outpoint: denom})
        return res

    def sort_outputs(self, tx):
        def sort_denoms_fn(o):
            if o.value == CREATE_COLLATERAL_VAL:
                rank = 0
            elif o.value in PS_DENOMS_VALS:
                rank = 1
            else:  # change
                rank = 2
            return (rank, o.value)
        tx._outputs.sort(key=sort_denoms_fn)

    # Workflow methods for pay collateral transaction
    def get_confirmed_ps_collateral_data(self):
        w = self.wallet
        for outpoint, ps_collateral in w.db.get_ps_collaterals().items():
            addr, value = ps_collateral
            utxos = w.get_utxos([addr], min_rounds=PSCoinRounds.COLLATERAL,
                                confirmed_only=True, consider_islocks=True)
            inputs = []
            for utxo in utxos:
                prev_h = utxo['prevout_hash']
                prev_n = utxo['prevout_n']
                if f'{prev_h}:{prev_n}' != outpoint:
                    continue
                w.add_input_info(utxo)
                inputs.append(utxo)
            if inputs:
                return outpoint, value, inputs
            else:
                self.logger.wfl_err(f'ps_collateral outpoint {outpoint}'
                                    f' is not confirmed')

    async def prepare_pay_collateral_wfl(self):
        try:
            _prepare = self._prepare_pay_collateral_tx
            res = await self.loop.run_in_executor(None, _prepare)
            if res:
                txid, wfl = res
                self.logger.wfl_ok(f'Completed pay collateral workflow with'
                                   f' tx: {txid}, workflow: {wfl.lid}')
                self.wallet.storage.write()
        except Exception as e:
            wfl = self.pay_collateral_wfl
            if wfl:
                self.logger.wfl_err(f'Error creating pay collateral tx:'
                                    f' {str(e)}, workflow: {wfl.lid}')
                await self.cleanup_pay_collateral_wfl(force=True)
            else:
                self.logger.wfl_err(f'Error during creation of pay collateral'
                                    f' worfklow: {str(e)}')
            type_e = type(e)
            msg = None
            if type_e == NoDynamicFeeEstimates:
                msg = self.NO_DYNAMIC_FEE_MSG.format(str(e))
            elif type_e == NotFoundInKeypairs:
                msg = self.NOT_FOUND_KEYS_MSG
            elif type_e == SignWithKeypairsFailed:
                msg = self.SIGN_WIHT_KP_FAILED_MSG
            if msg:
                await self.stop_mixing_from_async_thread(msg)

    def _prepare_pay_collateral_tx(self):
        with self.pay_collateral_wfl_lock:
            if self.pay_collateral_wfl:
                return
            uuid = str(uuid4())
            wfl = PSTxWorkflow(uuid=uuid)
            self.set_pay_collateral_wfl(wfl)
            self.logger.info(f'Started up pay collateral workflow: {wfl.lid}')

        res = self.get_confirmed_ps_collateral_data()
        if not res:
            raise Exception('No confirmed ps_collateral found')
        outpoint, value, inputs = res

        # check input addresses is in keypairs if keypairs cache available
        if self._keypairs_cache:
            input_addrs = [utxo['address'] for utxo in inputs]
            not_found_addrs = self._find_addrs_not_in_keypairs(input_addrs)
            if not_found_addrs:
                not_found_addrs = ', '.join(list(not_found_addrs))
                raise NotFoundInKeypairs(f'Input addresses is not found'
                                         f' in the keypairs cache:'
                                         f' {not_found_addrs} ')

        self.add_ps_spending_collateral(outpoint, wfl.uuid)
        if value >= COLLATERAL_VAL*2:
            ovalue = value - COLLATERAL_VAL
            output_addr = None
            for addr, data in self.wallet.db.get_ps_reserved().items():
                if data == outpoint:
                    output_addr = addr
                    break
            if not output_addr:
                reserved = self.reserve_addresses(1, for_change=True,
                                                  data=outpoint)
                output_addr = reserved[0]
            outputs = [TxOutput(TYPE_ADDRESS, output_addr, ovalue)]
        else:
            # OP_RETURN as ouptut script
            outputs = [TxOutput(TYPE_SCRIPT, '6a', 0)]

        tx = Transaction.from_io(inputs[:], outputs[:], locktime=0)
        tx.inputs()[0]['sequence'] = 0xffffffff
        tx = self.sign_transaction(tx, None)
        txid = tx.txid()
        raw_tx = tx.serialize_to_network()
        tx_type = PSTxTypes.PAY_COLLATERAL
        wfl.add_tx(txid=txid, raw_tx=raw_tx, tx_type=tx_type)
        wfl.completed = True
        with self.pay_collateral_wfl_lock:
            saved = self.pay_collateral_wfl
            if not saved:
                raise Exception('pay_collateral_wfl not found')
            if saved.uuid != wfl.uuid:
                raise Exception('pay_collateral_wfl differs from original')
            self.set_pay_collateral_wfl(wfl)
        return txid, wfl

    async def cleanup_pay_collateral_wfl(self, force=False):
        _cleanup = self._cleanup_pay_collateral_wfl
        changed = await self.loop.run_in_executor(None, _cleanup, force)
        if changed:
            self.wallet.storage.write()

    def _cleanup_pay_collateral_wfl(self, force=False):
        with self.pay_collateral_wfl_lock:
            wfl = self.pay_collateral_wfl
            if not wfl or wfl.completed and wfl.tx_order and not force:
                return
        w = self.wallet
        if wfl.tx_order:
            for txid in wfl.tx_order[::-1]:  # use reversed tx_order
                if w.db.get_transaction(txid):
                    w.remove_transaction(txid)
                else:
                    self._cleanup_pay_collateral_wfl_tx_data(txid)
        else:
            self._cleanup_pay_collateral_wfl_tx_data()
        return True

    def _cleanup_pay_collateral_wfl_tx_data(self, txid=None):
        with self.pay_collateral_wfl_lock:
            wfl = self.pay_collateral_wfl
            if not wfl:
                return
            if txid:
                tx_data = wfl.pop_tx(txid)
                if tx_data:
                    self.set_pay_collateral_wfl(wfl)
                    self.logger.info(f'Cleaned up pay collateral tx:'
                                     f' {txid}, workflow: {wfl.lid}')
        if wfl.tx_order:
            return

        w = self.wallet
        for outpoint, uuid in list(w.db.get_ps_spending_collaterals().items()):
            if uuid != wfl.uuid:
                continue
            with self.collateral_lock:
                self.pop_ps_spending_collateral(outpoint)

        with self.pay_collateral_wfl_lock:
            saved = self.pay_collateral_wfl
            if saved and saved.uuid == wfl.uuid:
                self.clear_pay_collateral_wfl()
        self.logger.info(f'Cleaned up pay collateral workflow: {wfl.lid}')

    def _search_pay_collateral_wfl(self, txid, tx):
        err = self._check_pay_collateral_tx_err(txid, tx, full_check=False)
        if not err:
            wfl = self.pay_collateral_wfl
            if wfl and wfl.tx_order and txid in wfl.tx_order:
                return wfl

    def _check_on_pay_collateral_wfl(self, txid, tx):
        wfl = self._search_pay_collateral_wfl(txid, tx)
        err = self._check_pay_collateral_tx_err(txid, tx)
        if not err:
            return True
        if wfl:
            raise AddPSDataError(f'{err}')
        else:
            return False

    def _process_by_pay_collateral_wfl(self, txid, tx):
        wfl = self._search_pay_collateral_wfl(txid, tx)
        if not wfl:
            return

        with self.pay_collateral_wfl_lock:
            saved = self.pay_collateral_wfl
            if not saved or saved.uuid != wfl.uuid:
                return
            tx_data = wfl.pop_tx(txid)
            if tx_data:
                self.set_pay_collateral_wfl(wfl)
                self.logger.wfl_done(f'Processed tx: {txid} from pay'
                                     f' collateral workflow: {wfl.lid}')
        if wfl.tx_order:
            return

        w = self.wallet
        for outpoint, uuid in list(w.db.get_ps_spending_collaterals().items()):
            if uuid != wfl.uuid:
                continue
            with self.collateral_lock:
                self.pop_ps_spending_collateral(outpoint)

        with self.pay_collateral_wfl_lock:
            saved = self.pay_collateral_wfl
            if saved and saved.uuid == wfl.uuid:
                self.clear_pay_collateral_wfl()
        self.logger.wfl_done(f'Finished processing of pay collateral'
                             f' workflow: {wfl.lid}')

    def get_pay_collateral_tx(self):
        wfl = self.pay_collateral_wfl
        if not wfl or not wfl.tx_order:
            return
        txid = wfl.tx_order[0]
        tx_data = wfl.tx_data.get(txid)
        if not tx_data:
            return
        return tx_data.raw_tx

    # Workflow methods for new collateral transaction
    def create_new_collateral_wfl_from_gui(self, coins, password):
        if self.state in self.mixing_running_states:
            return None, ('Can not create new collateral as mixing'
                          ' process is currently run.')
        wfl = self._start_new_collateral_wfl()
        if not wfl:
            return None, ('Can not create new collateral as other new'
                          ' collateral creation process is in progress')
        try:
            txid, tx = self._make_new_collateral_tx(wfl, coins, password)
            if not self.wallet.add_transaction(txid, tx):
                raise Exception(f'Transactiion with txid: {txid}'
                                f' conflicts with current history')
            with self.new_collateral_wfl_lock:
                saved = self.new_collateral_wfl
                if not saved:
                    raise Exception('new_collateral_wfl not found')
                if saved.uuid != wfl.uuid:
                    raise Exception('new_collateral_wfl differs from original')
                wfl.completed = True
                self.set_new_collateral_wfl(wfl)
                self.logger.wfl_ok(f'Completed new collateral workflow'
                                   f' with tx: {txid},'
                                   f' workflow: {wfl.lid}')
            return wfl, None
        except Exception as e:
            err = str(e)
            self.logger.wfl_err(f'Error creating new collateral tx:'
                                f' {err}, workflow: {wfl.lid}')
            self._cleanup_new_collateral_wfl(force=True)
            self.logger.info(f'Cleaned up new collateral workflow:'
                             f' {wfl.lid}')
            return None, err

    async def create_new_collateral_wfl(self):
        _start = self._start_new_collateral_wfl
        wfl = await self.loop.run_in_executor(None, _start)
        if not wfl:
            return
        try:
            _make_tx = self._make_new_collateral_tx
            txid, tx = await self.loop.run_in_executor(None, _make_tx, wfl)
            w = self.wallet
            # add_transaction need run in network therad
            if not w.add_transaction(txid, tx):
                raise Exception(f'Transactiion with txid: {txid}'
                                f' conflicts with current history')

            def _after_create_tx():
                with self.new_collateral_wfl_lock:
                    saved = self.new_collateral_wfl
                    if not saved:
                        raise Exception('new_collateral_wfl not found')
                    if saved.uuid != wfl.uuid:
                        raise Exception('new_collateral_wfl differs'
                                        ' from original')
                    wfl.completed = True
                    self.set_new_collateral_wfl(wfl)
                    self.logger.wfl_ok(f'Completed new collateral workflow'
                                       f' with tx: {txid},'
                                       f' workflow: {wfl.lid}')
            await self.loop.run_in_executor(None, _after_create_tx)
            w.storage.write()
        except Exception as e:
            self.logger.wfl_err(f'Error creating new collateral tx:'
                                f' {str(e)}, workflow: {wfl.lid}')
            await self.cleanup_new_collateral_wfl(force=True)
            type_e = type(e)
            msg = None
            if type_e == NoDynamicFeeEstimates:
                msg = self.NO_DYNAMIC_FEE_MSG.format(str(e))
            elif type_e == AddPSDataError:
                msg = self.ADD_PS_DATA_ERR_MSG
                type_name = SPEC_TX_NAMES[PSTxTypes.NEW_COLLATERAL]
                msg = f'{msg} {type_name} {txid}:\n{str(e)}'
            elif type_e == NotFoundInKeypairs:
                msg = self.NOT_FOUND_KEYS_MSG
            elif type_e == SignWithKeypairsFailed:
                msg = self.SIGN_WIHT_KP_FAILED_MSG
            elif type_e == NotEnoughFunds:
                msg = _('Insufficient funds to create collateral amount.'
                        ' You can use coin selector to manually create'
                        ' collateral amount from PrivateSend coins.')
            if msg:
                await self.stop_mixing_from_async_thread(msg)

    def _start_new_collateral_wfl(self):
        with self.new_collateral_wfl_lock:
            if self.new_collateral_wfl:
                return

            uuid = str(uuid4())
            wfl = PSTxWorkflow(uuid=uuid)
            self.set_new_collateral_wfl(wfl)
            self.logger.info(f'Started up new collateral workflow: {wfl.lid}')
            return self.new_collateral_wfl

    def _make_new_collateral_tx(self, wfl, coins=None, password=None):
        with self.pay_collateral_wfl_lock, \
                self.new_collateral_wfl_lock, \
                self.new_denoms_wfl_lock:
            if self.pay_collateral_wfl:
                raise Exception('Can not create new collateral as other new'
                                ' collateral amount seems to exists')
            if self.new_denoms_wfl:
                raise Exception('Can not create new collateral as new denoms'
                                ' creation process is in progress')
            if self.config is None:
                raise Exception('self.config is not set')

            if self.ps_collateral_cnt:
                raise Exception('Can not create new collateral as other new'
                                ' collateral amount exists')
            saved = self.new_collateral_wfl
            if not saved:
                raise Exception('new_collateral_wfl not found')
            if saved.uuid != wfl.uuid:
                raise Exception('new_collateral_wfl differs from original')

        # try to create new collateral tx with change outupt at first
        w = self.wallet
        uuid = wfl.uuid
        oaddr = self.reserve_addresses(1, data=uuid)[0]
        outputs = [TxOutput(TYPE_ADDRESS, oaddr, CREATE_COLLATERAL_VAL)]
        if coins is None:
            utxos = w.get_utxos(None,
                                excluded_addresses=w.frozen_addresses,
                                mature_only=True, confirmed_only=True,
                                consider_islocks=True)
            utxos = [utxo for utxo in utxos if not w.is_frozen_coin(utxo)]
        else:
            utxos = coins
        tx = w.make_unsigned_transaction(utxos, outputs, self.config)
        inputs = tx.inputs()
        # check input addresses is in keypairs if keypairs cache available
        if self._keypairs_cache:
            input_addrs = [utxo['address'] for utxo in inputs]
            not_found_addrs = self._find_addrs_not_in_keypairs(input_addrs)
            if not_found_addrs:
                raise NotFoundInKeypairs(f'Input addresses is not found'
                                         f' in the keypairs cache:'
                                         f' {not_found_addrs} ')

        # use first input address as a change, use selected inputs
        in0 = inputs[0]['address']
        tx = w.make_unsigned_transaction(inputs, outputs,
                                         self.config, change_addr=in0)
        # sort ouptus again (change last)
        self.sort_outputs(tx)
        tx = self.sign_transaction(tx, password)
        txid = tx.txid()
        raw_tx = tx.serialize_to_network()
        tx_type = PSTxTypes.NEW_COLLATERAL
        wfl.add_tx(txid=txid, raw_tx=raw_tx, tx_type=tx_type)
        with self.new_collateral_wfl_lock:
            saved = self.new_collateral_wfl
            if not saved:
                raise Exception('new_collateral_wfl not found')
            if saved.uuid != wfl.uuid:
                raise Exception('new_collateral_wfl differs from original')
            self.set_new_collateral_wfl(wfl)
        return txid, tx

    async def cleanup_new_collateral_wfl(self, force=False):
        _cleanup = self._cleanup_new_collateral_wfl
        changed = await self.loop.run_in_executor(None, _cleanup, force)
        if changed:
            self.wallet.storage.write()

    def _cleanup_new_collateral_wfl(self, force=False):
        with self.new_collateral_wfl_lock:
            wfl = self.new_collateral_wfl
            if not wfl or wfl.completed and wfl.tx_order and not force:
                return
        w = self.wallet
        if wfl.tx_order:
            for txid in wfl.tx_order[::-1]:  # use reversed tx_order
                if w.db.get_transaction(txid):
                    w.remove_transaction(txid)
                else:
                    self._cleanup_new_collateral_wfl_tx_data(txid)
        else:
            self._cleanup_new_collateral_wfl_tx_data()
        return True

    def _cleanup_new_collateral_wfl_tx_data(self, txid=None):
        with self.new_collateral_wfl_lock:
            wfl = self.new_collateral_wfl
            if not wfl:
                return
            if txid:
                tx_data = wfl.pop_tx(txid)
                if tx_data:
                    self.set_new_collateral_wfl(wfl)
                    self.logger.info(f'Cleaned up new collateral tx:'
                                     f' {txid}, workflow: {wfl.lid}')
        if wfl.tx_order:
            return

        w = self.wallet
        for addr in w.db.select_ps_reserved(data=wfl.uuid):
            w.db.pop_ps_reserved(addr)

        with self.new_collateral_wfl_lock:
            saved = self.new_collateral_wfl
            if saved and saved.uuid == wfl.uuid:
                self.clear_new_collateral_wfl()
        self.logger.info(f'Cleaned up new collateral workflow: {wfl.lid}')

    async def broadcast_new_collateral_wfl(self):
        def _check_wfl():
            with self.new_collateral_wfl_lock:
                wfl = self.new_collateral_wfl
                if not wfl:
                    return
                if not wfl.completed:
                    return
            return wfl
        wfl = await self.loop.run_in_executor(None, _check_wfl)
        if not wfl:
            return
        w = self.wallet
        tx_data = wfl.next_to_send(w)
        if not tx_data:
            return
        txid = tx_data.txid
        sent, err = await tx_data.send(self)
        if err:
            def _on_fail():
                with self.new_collateral_wfl_lock:
                    saved = self.new_collateral_wfl
                    if not saved:
                        raise Exception('new_collateral_wfl not found')
                    if saved.uuid != wfl.uuid:
                        raise Exception('new_collateral_wfl differs'
                                        ' from original')
                    self.set_new_collateral_wfl(wfl)
                self.logger.wfl_err(f'Failed broadcast of new collateral tx'
                                    f' {txid}: {err}, workflow {wfl.lid}')
            await self.loop.run_in_executor(None, _on_fail)
        if sent:
            def _on_success():
                with self.new_collateral_wfl_lock:
                    saved = self.new_collateral_wfl
                    if not saved:
                        raise Exception('new_collateral_wfl not found')
                    if saved.uuid != wfl.uuid:
                        raise Exception('new_collateral_wfl differs'
                                        ' from original')
                    self.set_new_collateral_wfl(wfl)
                self.logger.wfl_done(f'Broadcasted transaction {txid} from new'
                                     f' collateral workflow: {wfl.lid}')
                tx = Transaction(wfl.tx_data[txid].raw_tx)
                self._process_by_new_collateral_wfl(txid, tx)
                if not wfl.next_to_send(w):
                    self.logger.wfl_done(f'Broadcast completed for new'
                                         f' collateral workflow: {wfl.lid}')
            await self.loop.run_in_executor(None, _on_success)

    def _search_new_collateral_wfl(self, txid, tx):
        err = self._check_new_collateral_tx_err(txid, tx, full_check=False)
        if not err:
            wfl = self.new_collateral_wfl
            if wfl and wfl.tx_order and txid in wfl.tx_order:
                return wfl

    def _check_on_new_collateral_wfl(self, txid, tx):
        wfl = self._search_new_collateral_wfl(txid, tx)
        err = self._check_new_collateral_tx_err(txid, tx)
        if not err:
            return True
        if wfl:
            raise AddPSDataError(f'{err}')
        else:
            return False

    def _process_by_new_collateral_wfl(self, txid, tx):
        wfl = self._search_new_collateral_wfl(txid, tx)
        if not wfl:
            return

        with self.new_collateral_wfl_lock:
            saved = self.new_collateral_wfl
            if not saved or saved.uuid != wfl.uuid:
                return
            tx_data = wfl.pop_tx(txid)
            if tx_data:
                self.set_new_collateral_wfl(wfl)
                self.logger.wfl_done(f'Processed tx: {txid} from new'
                                     f' collateral workflow: {wfl.lid}')
        if wfl.tx_order:
            return

        w = self.wallet
        for addr in w.db.select_ps_reserved(data=wfl.uuid):
            w.db.pop_ps_reserved(addr)

        with self.new_collateral_wfl_lock:
            saved = self.new_collateral_wfl
            if saved and saved.uuid == wfl.uuid:
                self.clear_new_collateral_wfl()
        self.logger.wfl_done(f'Finished processing of new collateral'
                             f' workflow: {wfl.lid}')

    # Workflow methods for new denoms transaction
    def create_new_denoms_wfl_from_gui(self, coins, password):
        if self.state in self.mixing_running_states:
            return None, ('Can not create new denoms as mixing process'
                          ' is currently run.')
        wfl, outputs_amounts = self._start_new_denoms_wfl(coins)
        if not outputs_amounts:
            return None, ('Can not create new denoms,'
                          ' not enough coins selected')
        if not wfl:
            return None, ('Can not create new denoms as other new'
                          ' denoms creation process is in progress')
        last_tx_idx = len(outputs_amounts) - 1
        w = self.wallet
        for i, tx_amounts in enumerate(outputs_amounts):
            try:
                txid, tx = self._make_new_denoms_tx(wfl, tx_amounts, i,
                                                    coins, password)
                if not w.add_transaction(txid, tx):
                    raise Exception(f'Transactiion with txid: {txid}'
                                    f' conflicts with current history')
                if i == last_tx_idx:
                    with self.new_denoms_wfl_lock:
                        saved = self.new_denoms_wfl
                        if not saved:
                            raise Exception('new_denoms_wfl not found')
                        if saved.uuid != wfl.uuid:
                            raise Exception('new_denoms_wfl differs'
                                            ' from original')
                        wfl.completed = True
                        self.set_new_denoms_wfl(wfl)
                        self.logger.wfl_ok(f'Completed new denoms'
                                           f' workflow: {wfl.lid}')
                    return wfl, None
                else:
                    prev_outputs = tx.outputs()
                    c_prev_outputs = len(prev_outputs)
                    addr = prev_outputs[-1].address
                    utxos = w.get_utxos([addr], min_rounds=PSCoinRounds.OTHER)
                    last_outpoint = f'{txid}:{c_prev_outputs-1}'
                    coins = []
                    for utxo in utxos:
                        prev_h = utxo['prevout_hash']
                        prev_n = utxo['prevout_n']
                        if f'{prev_h}:{prev_n}' != last_outpoint:
                            continue
                        coins.append(utxo)
            except Exception as e:
                err = str(e)
                self.logger.wfl_err(f'Error creating new denoms tx:'
                                    f' {err}, workflow: {wfl.lid}')
                self._cleanup_new_denoms_wfl(force=True)
                self.logger.info(f'Cleaned up new denoms workflow:'
                                 f' {wfl.lid}')
                return None, err

    async def create_new_denoms_wfl(self):
        _start = self._start_new_denoms_wfl
        wfl, outputs_amounts = await self.loop.run_in_executor(None, _start)
        if not wfl:
            return
        last_tx_idx = len(outputs_amounts) - 1
        for i, tx_amounts in enumerate(outputs_amounts):
            try:
                w = self.wallet

                def _check_enough_funds():
                    total = sum([sum(a) for a in outputs_amounts])
                    total += CREATE_COLLATERAL_VAL*3
                    coins = w.get_utxos(None,
                                        excluded_addresses=w.frozen_addresses,
                                        mature_only=True, confirmed_only=True,
                                        consider_islocks=True)
                    coins_total = sum([c['value'] for c in coins
                                       if not w.is_frozen_coin(c)])
                    if coins_total < total:
                        raise NotEnoughFunds()
                if i == 0:
                    await self.loop.run_in_executor(None, _check_enough_funds)
                _make_tx = self._make_new_denoms_tx
                txid, tx = await self.loop.run_in_executor(None, _make_tx,
                                                           wfl, tx_amounts, i)
                # add_transaction need run in network therad
                if not w.add_transaction(txid, tx):
                    raise Exception(f'Transaction with txid: {txid}'
                                    f' conflicts with current history')

                def _after_create_tx():
                    with self.new_denoms_wfl_lock:
                        self.logger.info(f'Created new denoms tx: {txid},'
                                         f' workflow: {wfl.lid}')
                        if i == last_tx_idx:
                            saved = self.new_denoms_wfl
                            if not saved:
                                raise Exception('new_denoms_wfl not found')
                            if saved.uuid != wfl.uuid:
                                raise Exception('new_denoms_wfl differs'
                                                ' from original')
                            wfl.completed = True
                            self.set_new_denoms_wfl(wfl)
                            self.logger.wfl_ok(f'Completed new denoms'
                                               f' workflow: {wfl.lid}')
                await self.loop.run_in_executor(None, _after_create_tx)
                w.storage.write()
            except Exception as e:
                self.logger.wfl_err(f'Error creating new denoms tx:'
                                    f' {str(e)}, workflow: {wfl.lid}')
                await self.cleanup_new_denoms_wfl(force=True)
                type_e = type(e)
                msg = None
                if type_e == NoDynamicFeeEstimates:
                    msg = self.NO_DYNAMIC_FEE_MSG.format(str(e))
                elif type_e == AddPSDataError:
                    msg = self.ADD_PS_DATA_ERR_MSG
                    type_name = SPEC_TX_NAMES[PSTxTypes.NEW_DENOMS]
                    msg = f'{msg} {type_name} {txid}:\n{str(e)}'
                elif type_e == NotFoundInKeypairs:
                    msg = self.NOT_FOUND_KEYS_MSG
                elif type_e == SignWithKeypairsFailed:
                    msg = self.SIGN_WIHT_KP_FAILED_MSG
                elif type_e == NotEnoughFunds:
                    msg = _('Insufficient funds to create anonymized amount.'
                            ' You can use PrivateSend settings to change'
                            ' amount of Dash to keep anonymized.')
                if msg:
                    await self.stop_mixing_from_async_thread(msg)
                break

    def _start_new_denoms_wfl(self, coins=None):
        outputs_amounts = self.calc_need_denoms_amounts(coins=coins)
        if not outputs_amounts:
            return None, None
        with self.new_denoms_wfl_lock, \
                self.pay_collateral_wfl_lock, \
                self.new_collateral_wfl_lock:
            if self.new_denoms_wfl:
                return None, None

            if (not self.pay_collateral_wfl
                    and not self.new_collateral_wfl
                    and not self.ps_collateral_cnt):
                outputs_amounts[0].insert(0, CREATE_COLLATERAL_VAL)

            uuid = str(uuid4())
            wfl = PSTxWorkflow(uuid=uuid)
            self.set_new_denoms_wfl(wfl)
            self.logger.info(f'Started up new denoms workflow: {wfl.lid}')
            return wfl, outputs_amounts

    def _make_new_denoms_tx(self, wfl, tx_amounts, i,
                            coins=None, password=None):
        if self.config is None:
            raise Exception('self.config is not set')

        w = self.wallet
        # try to create new denoms tx with change outupt at first
        use_confirmed = (i == 0)  # for first tx use confirmed coins
        addrs_cnt = len(tx_amounts)
        oaddrs = self.reserve_addresses(addrs_cnt, data=wfl.uuid)
        outputs = [TxOutput(TYPE_ADDRESS, addr, a)
                   for addr, a in zip(oaddrs, tx_amounts)]
        if coins is None:
            utxos = w.get_utxos(None,
                                excluded_addresses=w.frozen_addresses,
                                mature_only=True,
                                confirmed_only=use_confirmed,
                                consider_islocks=True)
            utxos = [utxo for utxo in utxos if not w.is_frozen_coin(utxo)]
        else:
            utxos = coins
        tx = w.make_unsigned_transaction(utxos, outputs, self.config)
        inputs = tx.inputs()
        # check input addresses is in keypairs if keypairs cache available
        if self._keypairs_cache:
            input_addrs = [utxo['address'] for utxo in inputs]
            not_found_addrs = self._find_addrs_not_in_keypairs(input_addrs)
            if not_found_addrs:
                raise NotFoundInKeypairs(f'Input addresses is not found'
                                         f' in the keypairs cache:'
                                         f' {not_found_addrs} ')

        # use first input address as a change, use selected inputs
        in0 = inputs[0]['address']
        tx = w.make_unsigned_transaction(inputs, outputs,
                                         self.config, change_addr=in0)
        # sort ouptus again (change last)
        self.sort_outputs(tx)
        tx = self.sign_transaction(tx, password)
        txid = tx.txid()
        raw_tx = tx.serialize_to_network()
        tx_type = PSTxTypes.NEW_DENOMS
        wfl.add_tx(txid=txid, raw_tx=raw_tx, tx_type=tx_type)
        with self.new_denoms_wfl_lock:
            saved = self.new_denoms_wfl
            if not saved:
                raise Exception('new_denoms_wfl not found')
            if saved.uuid != wfl.uuid:
                raise Exception('new_denoms_wfl differs from original')
            self.set_new_denoms_wfl(wfl)
        return txid, tx

    async def cleanup_new_denoms_wfl(self, force=False):
        _cleanup = self._cleanup_new_denoms_wfl
        changed = await self.loop.run_in_executor(None, _cleanup, force)
        if changed:
            self.wallet.storage.write()

    def _cleanup_new_denoms_wfl(self, force=False):
        with self.new_denoms_wfl_lock:
            wfl = self.new_denoms_wfl
            if not wfl or wfl.completed and wfl.tx_order and not force:
                return
        w = self.wallet
        if wfl.tx_order:
            for txid in wfl.tx_order[::-1]:  # use reversed tx_order
                if w.db.get_transaction(txid):
                    w.remove_transaction(txid)
                else:
                    self._cleanup_new_denoms_wfl_tx_data(txid)
        else:
            self._cleanup_new_denoms_wfl_tx_data()
        return True

    def _cleanup_new_denoms_wfl_tx_data(self, txid=None):
        with self.new_denoms_wfl_lock:
            wfl = self.new_denoms_wfl
            if not wfl:
                return
            if txid:
                tx_data = wfl.pop_tx(txid)
                if tx_data:
                    self.set_new_denoms_wfl(wfl)
                    self.logger.info(f'Cleaned up new denoms tx:'
                                     f' {txid}, workflow: {wfl.lid}')
        if wfl.tx_order:
            return

        w = self.wallet
        for addr in w.db.select_ps_reserved(data=wfl.uuid):
            w.db.pop_ps_reserved(addr)

        with self.new_denoms_wfl_lock:
            saved = self.new_denoms_wfl
            if saved and saved.uuid == wfl.uuid:
                self.clear_new_denoms_wfl()
        self.logger.info(f'Cleaned up new denoms workflow: {wfl.lid}')

    async def broadcast_new_denoms_wfl(self):
        def _check_wfl():
            with self.new_denoms_wfl_lock:
                wfl = self.new_denoms_wfl
                if not wfl:
                    return
                if not wfl.completed:
                    return
            return wfl
        wfl = await self.loop.run_in_executor(None, _check_wfl)
        if not wfl:
            return
        w = self.wallet
        tx_data = wfl.next_to_send(w)
        if not tx_data:
            return
        txid = tx_data.txid
        sent, err = await tx_data.send(self)
        if err:
            def _on_fail():
                with self.new_denoms_wfl_lock:
                    saved = self.new_denoms_wfl
                    if not saved:
                        raise Exception('new_denoms_wfl not found')
                    if saved.uuid != wfl.uuid:
                        raise Exception('new_denoms_wfl differs from original')
                    self.set_new_denoms_wfl(wfl)
                self.logger.wfl_err(f'Failed broadcast of new denoms tx'
                                    f' {txid}: {err}, workflow {wfl.lid}')
            await self.loop.run_in_executor(None, _on_fail)
        if sent:
            def _on_success():
                with self.new_denoms_wfl_lock:
                    saved = self.new_denoms_wfl
                    if not saved:
                        raise Exception('new_denoms_wfl not found')
                    if saved.uuid != wfl.uuid:
                        raise Exception('new_denoms_wfl differs from original')
                    self.set_new_denoms_wfl(wfl)
                self.logger.wfl_done(f'Broadcasted transaction {txid} from new'
                                     f' denoms workflow: {wfl.lid}')
                tx = Transaction(wfl.tx_data[txid].raw_tx)
                self._process_by_new_denoms_wfl(txid, tx)
                if not wfl.next_to_send(w):
                    self.logger.wfl_done(f'Broadcast completed for new denoms'
                                         f' workflow: {wfl.lid}')
            await self.loop.run_in_executor(None, _on_success)

    def _search_new_denoms_wfl(self, txid, tx):
        err = self._check_new_denoms_tx_err(txid, tx, full_check=False)
        if not err:
            wfl = self.new_denoms_wfl
            if wfl and wfl.tx_order and txid in wfl.tx_order:
                return wfl

    def _check_on_new_denoms_wfl(self, txid, tx):
        wfl = self._search_new_denoms_wfl(txid, tx)
        err = self._check_new_denoms_tx_err(txid, tx)
        if not err:
            return True
        if wfl:
            raise AddPSDataError(f'{err}')
        else:
            return False

    def _process_by_new_denoms_wfl(self, txid, tx):
        wfl = self._search_new_denoms_wfl(txid, tx)
        if not wfl:
            return

        with self.new_denoms_wfl_lock:
            saved = self.new_denoms_wfl
            if not saved or saved.uuid != wfl.uuid:
                return
            tx_data = wfl.pop_tx(txid)
            if tx_data:
                self.set_new_denoms_wfl(wfl)
                self.logger.wfl_done(f'Processed tx: {txid} from new denoms'
                                     f' workflow: {wfl.lid}')
        if wfl.tx_order:
            return

        w = self.wallet
        for addr in w.db.select_ps_reserved(data=wfl.uuid):
            w.db.pop_ps_reserved(addr)

        with self.new_denoms_wfl_lock:
            saved = self.new_denoms_wfl
            if saved and saved.uuid == wfl.uuid:
                self.clear_new_denoms_wfl()
        self.logger.wfl_done(f'Finished processing of new denoms'
                             f' workflow: {wfl.lid}')

    # Workflow methods for denominate transaction
    async def cleanup_staled_denominate_wfls(self):
        def _cleanup_staled():
            changed = False
            for uuid in self.denominate_wfl_list:
                wfl = self.get_denominate_wfl(uuid)
                if not wfl or not wfl.completed:
                    continue
                now = time.time()
                if now - wfl.completed > WAIT_FOR_MN_TXS_TIME_SEC:
                    self.logger.info(f'Cleaning staled denominate'
                                     f' workflow: {wfl.lid}')
                    self._cleanup_denominate_wfl(wfl)
                    changed = True
            return changed
        while True:
            changed = await self.loop.run_in_executor(None, _cleanup_staled)
            if changed:
                self.wallet.storage.write()
            await asyncio.sleep(WAIT_FOR_MN_TXS_TIME_SEC/12)

    async def start_denominate_wfl(self):
        wfl = None
        try:
            _start = self._start_denominate_wfl
            dsq = None
            session = None
            if random.random() > 0.33:
                self.logger.debug(f'try to get masternode from recent dsq')
                while not self.main_taskgroup.closed():
                    recent_mns = self.recent_mixes_mns
                    dsq = await self.dash_net.get_recent_dsq(recent_mns)
                    self.logger.debug(f'get dsq from recent dsq queue'
                                     f' {dsq.masternodeOutPoint}')
                    dval = PS_DENOM_REVERSE_DICT[dsq.nDenom]
                    wfl = await self.loop.run_in_executor(None, _start, dval)
                    break
            else:
                self.logger.debug(f'try to create new queue'
                                  f' on random masternode')
                wfl = await self.loop.run_in_executor(None, _start)
            if not wfl:
                return

            session = await self.start_mix_session(wfl.denom, dsq, wfl.lid)

            pay_collateral_tx = self.get_pay_collateral_tx()
            if not pay_collateral_tx:
                raise Exception('Absent suitable pay collateral tx')
            await session.send_dsa(pay_collateral_tx)
            while True:
                cmd, res = await session.read_next_msg(wfl)
                if cmd == 'dssu':
                    continue
                elif cmd == 'dsq' and session.fReady:
                    break
                else:
                    raise Exception(f'Unsolisited cmd: {cmd} after dsa sent')

            pay_collateral_tx = self.get_pay_collateral_tx()
            if not pay_collateral_tx:
                raise Exception('Absent suitable pay collateral tx')

            final_tx = None
            await session.send_dsi(wfl.inputs, pay_collateral_tx, wfl.outputs)
            while True:
                cmd, res = await session.read_next_msg(wfl)
                if cmd == 'dssu':
                    continue
                elif cmd == 'dsf':
                    final_tx = res
                    break
                else:
                    raise Exception(f'Unsolisited cmd: {cmd} after dsi sent')

            signed_inputs = self._sign_inputs(final_tx, wfl.inputs)
            await session.send_dss(signed_inputs)
            while True:
                cmd, res = await session.read_next_msg(wfl)
                if cmd == 'dssu':
                    continue
                elif cmd == 'dsc':
                    def _on_dsc():
                        with self.denominate_wfl_lock:
                            saved = self.get_denominate_wfl(wfl.uuid)
                            if saved:
                                saved.completed = time.time()
                                self.set_denominate_wfl(saved)
                                return saved
                            else:  # already processed from _add_ps_data
                                self.logger.debug(f'denominate workflow:'
                                                  f' {wfl.lid} not found')
                    saved = await self.loop.run_in_executor(None, _on_dsc)
                    if saved:
                        wfl = saved
                        self.wallet.storage.write()
                    break
                else:
                    raise Exception(f'Unsolisited cmd: {cmd} after dss sent')
            self.logger.wfl_ok(f'Completed denominate workflow: {wfl.lid}')
        except Exception as e:
            type_e = type(e)
            if type_e != asyncio.CancelledError:
                if wfl:
                    self.logger.wfl_err(f'Error in denominate worfklow:'
                                        f' {str(e)}, workflow: {wfl.lid}')
                else:
                    self.logger.wfl_err(f'Error during creation of denominate'
                                        f' worfklow: {str(e)}')
                msg = None
                if type_e == NoDynamicFeeEstimates:
                    msg = self.NO_DYNAMIC_FEE_MSG.format(str(e))
                elif type_e == NotFoundInKeypairs:
                    msg = self.NOT_FOUND_KEYS_MSG
                elif type_e == SignWithKeypairsFailed:
                    msg = self.SIGN_WIHT_KP_FAILED_MSG
                if msg:
                    await self.stop_mixing_from_async_thread(msg)
        finally:
            if session:
                await self.stop_mix_session(session.peer_str)
            if wfl:
                await self.cleanup_denominate_wfl(wfl)

    def _select_denoms_to_mix(self, denom_value=None):
        if not self._denoms_to_mix_cache:
            self.logger.debug(f'No suitable denoms to mix,'
                              f' _denoms_to_mix_cache is empty')
            return None, None, None

        if denom_value is not None:
            denoms = self.denoms_to_mix(denom_value=denom_value)
        else:
            denoms = self.denoms_to_mix()
        outpoints = list(denoms.keys())

        w = self.wallet
        icnt = 0
        txids = []
        inputs = []
        denom_rounds = None
        while icnt < random.randint(1, PRIVATESEND_ENTRY_MAX_SIZE):
            if not outpoints:
                break

            outpoint = outpoints.pop(random.randint(0, len(outpoints)-1))
            if not w.db.get_ps_denom(outpoint):  # already spent
                continue

            if w.db.get_ps_spending_denom(outpoint):  # reserved to spend
                continue

            txid = outpoint.split(':')[0]
            if txid in txids:  # skip outputs from same tx
                continue

            height = w.get_tx_height(txid).height
            islock = w.db.get_islock(txid)
            if not islock and height <= 0:  # skip not islocked/confirmed
                continue

            denom = denoms.pop(outpoint)
            if denom[2] >= self.mix_rounds:
                continue

            if denom_value is None:
                denom_value = denom[1]
            elif denom[1] != denom_value:  # skip other denom values
                continue

            if denom_rounds is None:
                denom_rounds = denom[2]

            inputs.append(outpoint)
            txids.append(txid)
            icnt += 1

        if not inputs:
            self.logger.debug(f'No suitable denoms to mix:'
                              f' denom_value={denom_value},'
                              f' denom_rounds={denom_rounds}')
            return None, None, None
        else:
            return inputs, denom_value, denom_rounds

    def _start_denominate_wfl(self, denom_value=None):
        if self.active_denominate_wfl_cnt >= self.max_sessions:
            return
        selected_inputs, denom_value, denom_rounds = \
            self._select_denoms_to_mix(denom_value)
        if not selected_inputs:
            return

        with self.denominate_wfl_lock, self.denoms_lock:
            if self.active_denominate_wfl_cnt >= self.max_sessions:
                return
            icnt = 0
            inputs = []
            input_addrs = []
            w = self.wallet
            for outpoint in selected_inputs:
                denom = w.db.get_ps_denom(outpoint)
                if not denom:
                    continue  # already spent
                if w.db.get_ps_spending_denom(outpoint):
                    continue  # already used by other wfl
                inputs.append(outpoint)
                input_addrs.append(denom[0])
                icnt += 1

            if icnt < 1:
                self.logger.debug(f'No suitable denoms to mix after'
                                  f' denoms_lock: denom_value={denom_value},'
                                  f' denom_rounds={denom_rounds}')
                return

            uuid = str(uuid4())
            wfl = PSDenominateWorkflow(uuid=uuid)
            wfl.inputs = inputs
            wfl.denom = denom_value
            wfl.rounds = denom_rounds
            self.set_denominate_wfl(wfl)
            for outpoint in inputs:
                self.add_ps_spending_denom(outpoint, wfl.uuid)

        # check input addresses is in keypairs if keypairs cache available
        if self._keypairs_cache:
            not_found_addrs = self._find_addrs_not_in_keypairs(input_addrs)
            if not_found_addrs:
                raise NotFoundInKeypairs(f'Input addresses is not found'
                                         f' in the keypairs cache:'
                                         f' {not_found_addrs} ')

        output_addrs = []
        found_outpoints = []
        for addr, data in w.db.get_ps_reserved().items():
            if data in inputs:
                output_addrs.append(addr)
                found_outpoints.append(data)
        for outpoint in inputs:
            if outpoint not in found_outpoints:
                reserved = self.reserve_addresses(1, data=outpoint)
                output_addrs.append(reserved[0])

        with self.denominate_wfl_lock:
            saved = self.get_denominate_wfl(wfl.uuid)
            if not saved:
                raise Exception('denominate_wfl {wfl.lid} not found')
            wfl = saved
            wfl.outputs = output_addrs
            self.set_denominate_wfl(saved)

        self.logger.info(f'Created denominate workflow: {wfl.lid}, with inputs'
                         f' value {wfl.denom}, rounds'
                         f' {wfl.rounds}, count {len(wfl.inputs)}')
        return wfl

    def _sign_inputs(self, tx, inputs):
        signed_inputs = []
        tx = self._sign_denominate_tx(tx)
        for i in tx.inputs():
            prev_h = i['prevout_hash']
            prev_n = i['prevout_n']
            if f'{prev_h}:{prev_n}' not in inputs:
                continue
            prev_h = bfh(prev_h)[::-1]
            prev_n = int(prev_n)
            scriptSig = bfh(i['scriptSig'])
            sequence = i['sequence']
            signed_inputs.append(CTxIn(prev_h, prev_n, scriptSig, sequence))
        return signed_inputs

    def _sign_denominate_tx(self, tx):
        w = self.wallet
        mine_txins_cnt = 0
        for txin in tx.inputs():
            w.add_input_info(txin)
            if txin['address'] is None:
                del txin['num_sig']
                txin['x_pubkeys'] = []
                txin['pubkeys'] = []
                txin['signatures'] = []
                continue
            mine_txins_cnt += 1
        self.sign_transaction(tx, None, mine_txins_cnt)
        raw_tx = tx.serialize()
        return Transaction(raw_tx)

    async def cleanup_denominate_wfl(self, wfl):
        _cleanup = self._cleanup_denominate_wfl
        changed = await self.loop.run_in_executor(None, _cleanup, wfl)
        if changed:
            self.wallet.storage.write()

    def _cleanup_denominate_wfl(self, wfl):
        with self.denominate_wfl_lock:
            saved = self.get_denominate_wfl(wfl.uuid)
            if not saved:  # already processed from _add_ps_data
                return
            else:
                wfl = saved

            completed = wfl.completed
            if completed:
                now = time.time()
                if now - wfl.completed <= WAIT_FOR_MN_TXS_TIME_SEC:
                    return

        w = self.wallet
        for outpoint, uuid in list(w.db.get_ps_spending_denoms().items()):
            if uuid != wfl.uuid:
                continue
            with self.denoms_lock:
                self.pop_ps_spending_denom(outpoint)

        with self.denominate_wfl_lock:
            self.clear_denominate_wfl(wfl.uuid)
        self.logger.info(f'Cleaned up denominate workflow: {wfl.lid}')
        return True

    def _search_denominate_wfl(self, txid, tx):
        err = self._check_denominate_tx_err(txid, tx, full_check=False)
        if not err:
            for uuid in self.denominate_wfl_list:
                wfl = self.get_denominate_wfl(uuid)
                if not wfl or not wfl.completed:
                    continue
                if self._check_denominate_tx_io_on_wfl(txid, tx, wfl):
                    return wfl

    def _check_on_denominate_wfl(self, txid, tx):
        wfl = self._search_denominate_wfl(txid, tx)
        err = self._check_denominate_tx_err(txid, tx)
        if not err:
            return True
        if wfl:
            raise AddPSDataError(f'{err}')
        else:
            return False

    def _process_by_denominate_wfl(self, txid, tx):
        wfl = self._search_denominate_wfl(txid, tx)
        if not wfl:
            return

        w = self.wallet
        for outpoint, uuid in list(w.db.get_ps_spending_denoms().items()):
            if uuid != wfl.uuid:
                continue
            with self.denoms_lock:
                self.pop_ps_spending_denom(outpoint)

        with self.denominate_wfl_lock:
            self.clear_denominate_wfl(wfl.uuid)
        self.logger.wfl_done(f'Finished processing of denominate'
                             f' workflow: {wfl.lid} with tx: {txid}')

    def get_workflow_tx_info(self, wfl):
        w = self.wallet
        tx_cnt = len(wfl.tx_order)
        tx_type = None if not tx_cnt else wfl.tx_data[wfl.tx_order[0]].tx_type
        total = 0
        total_fee = 0
        for txid in wfl.tx_order:
            tx = Transaction(wfl.tx_data[txid].raw_tx)
            tx_info = w.get_tx_info(tx)
            total += tx_info.amount
            total_fee += tx_info.fee
        return tx_type, tx_cnt, total, total_fee

    # Methods to check different tx types, add/rm ps data on these types
    def unpack_io_values(func):
        '''Decorator to prepare tx inputs/outputs info'''
        def func_wrapper(self, txid, tx, full_check=True):
            w = self.wallet
            inputs = []
            outputs = []
            icnt = mine_icnt = others_icnt = 0
            ocnt = op_return_ocnt = 0
            for i in tx.inputs():
                icnt += 1
                prev_h = i['prevout_hash']
                prev_n = i['prevout_n']
                prev_tx = w.db.get_transaction(prev_h)
                if prev_tx:
                    o = prev_tx.outputs()[prev_n]
                    if w.is_mine(o.address):
                        inputs.append((o, prev_h, prev_n, True))  # mine
                        mine_icnt += 1
                    else:
                        inputs.append((o, prev_h, prev_n, False))  # others
                        others_icnt += 1
                else:
                    inputs.append((None, prev_h, prev_n, False))  # others
                    others_icnt += 1
            for idx, o in enumerate(tx.outputs()):
                ocnt += 1
                if o.address.lower() == '6a':
                    op_return_ocnt += 1
                outputs.append((o, txid, idx))
            io_values = (inputs, outputs,
                         icnt, mine_icnt, others_icnt, ocnt, op_return_ocnt)
            return func(self, txid, io_values, full_check)
        return func_wrapper

    def _add_spent_ps_outpoints_ps_data(self, txid, tx):
        w = self.wallet
        spent_ps_addrs = set()
        for txin in tx.inputs():
            spent_prev_h = txin['prevout_hash']
            spent_prev_n = txin['prevout_n']
            spent_outpoint = f'{spent_prev_h}:{spent_prev_n}'

            with self.denoms_lock:
                spent_denom = w.db.get_ps_spent_denom(spent_outpoint)
                if not spent_denom:
                    spent_denom = w.db.get_ps_denom(spent_outpoint)
                    if spent_denom:
                        w.db.add_ps_spent_denom(spent_outpoint, spent_denom)
                        spent_ps_addrs.add(spent_denom[0])
                # cleanup of denominate wfl will be done on timeout
                self.pop_ps_denom(spent_outpoint)

            with self.collateral_lock:
                spent_collateral = w.db.get_ps_spent_collateral(spent_outpoint)
                if not spent_collateral:
                    spent_collateral = w.db.get_ps_collateral(spent_outpoint)
                    if spent_collateral:
                        w.db.add_ps_spent_collateral(spent_outpoint,
                                                     spent_collateral)
                        spent_ps_addrs.add(spent_collateral[0])
                # cleanup of pay collateral wfl
                uuid = w.db.get_ps_spending_collateral(spent_outpoint)
                if uuid:
                    with self.pay_collateral_wfl_lock:
                        saved = self.pay_collateral_wfl
                        if saved and saved.uuid == uuid:
                            self.clear_pay_collateral_wfl()
                w.db.pop_ps_collateral(spent_outpoint)

            with self.others_lock:
                spent_other = w.db.get_ps_spent_other(spent_outpoint)
                if not spent_other:
                    spent_other = w.db.get_ps_other(spent_outpoint)
                    if spent_other:
                        w.db.add_ps_spent_other(spent_outpoint, spent_other)
                        spent_ps_addrs.add(spent_other[0])
                w.db.pop_ps_other(spent_outpoint)
        self.add_spent_addrs(spent_ps_addrs)

    def _rm_spent_ps_outpoints_ps_data(self, txid, tx):
        w = self.wallet
        restored_ps_addrs = set()
        for txin in tx.inputs():
            restore_prev_h = txin['prevout_hash']
            restore_prev_n = txin['prevout_n']
            restore_outpoint = f'{restore_prev_h}:{restore_prev_n}'
            tx_type, completed = w.db.get_ps_tx_removed(restore_prev_h)
            with self.denoms_lock:
                if not tx_type:
                    restore_denom = w.db.get_ps_denom(restore_outpoint)
                    if not restore_denom:
                        restore_denom = \
                            w.db.get_ps_spent_denom(restore_outpoint)
                        if restore_denom:
                            self.add_ps_denom(restore_outpoint, restore_denom)
                            restored_ps_addrs.add(restore_denom[0])
                w.db.pop_ps_spent_denom(restore_outpoint)

            with self.collateral_lock:
                if not tx_type:
                    restore_collateral = \
                        w.db.get_ps_collateral(restore_outpoint)
                    if not restore_collateral:
                        restore_collateral = \
                            w.db.get_ps_spent_collateral(restore_outpoint)
                        if restore_collateral:
                            w.db.add_ps_collateral(restore_outpoint,
                                                   restore_collateral)
                            restored_ps_addrs.add(restore_collateral[0])
                w.db.pop_ps_spent_collateral(restore_outpoint)

            with self.others_lock:
                if not tx_type:
                    restore_other = w.db.get_ps_other(restore_outpoint)
                    if not restore_other:
                        restore_other = \
                            w.db.get_ps_spent_other(restore_outpoint)
                        if restore_other:
                            w.db.add_ps_other(restore_outpoint, restore_other)
                            restored_ps_addrs.add(restore_other[0])
                w.db.pop_ps_spent_other(restore_outpoint)
        self.restore_spent_addrs(restored_ps_addrs)

    @unpack_io_values
    def _check_new_denoms_tx_err(self, txid, io_values, full_check):
        (inputs, outputs,
         icnt, mine_icnt, others_icnt, ocnt, op_return_ocnt) = io_values
        if others_icnt > 0:
            return 'Transaction has not mine inputs'
        if op_return_ocnt > 0:
            return 'Transaction has OP_RETURN outputs'
        if mine_icnt == 0:
            return 'Transaction has not enough inputs count'

        if not full_check:
            return

        o_last, o_prev_h, o_prev_n = outputs[-1]
        i_first, i_prev_h, i_prev_n, is_mine = inputs[0]
        if o_last.address == i_first.address:  # seems it is change value
            denoms_outputs = outputs[:-1]
        elif o_last.value in PS_DENOMS_VALS:  # maybe no change happens
            denoms_outputs = outputs
        else:
            return f'Unsuitable last output value={o_last.value}'
        dval_cnt = 0
        collateral_count = 0
        denoms_cnt = 0
        last_denom_val = PS_DENOMS_VALS[0]  # must start with minimal denom
        for o, prev_h, prev_n in denoms_outputs:
            val = o.value
            addr = o.address
            if val not in PS_DENOMS_VALS:
                if collateral_count > 0:  # one collateral already found
                    return f'Unsuitable output value={val}'
                if val == CREATE_COLLATERAL_VAL:
                    collateral_count += 1
                continue
            elif val < last_denom_val:  # must increase or be the same
                return (f'Unsuitable denom value={val}, must be'
                        f' {last_denom_val} or greater')
            elif val == last_denom_val:
                dval_cnt += 1
                if dval_cnt > 11:  # max 11 times of same denom val
                    return f'To many denoms of value={val}'
            else:
                dval_cnt = 1
                last_denom_val = val
            denoms_cnt += 1
        if denoms_cnt == 0:
            return 'Transaction has no denoms'

    def _add_new_denoms_ps_data(self, txid, tx):
        w = self.wallet
        self._add_spent_ps_outpoints_ps_data(txid, tx)
        outputs = tx.outputs()
        last_ouput_idx = len(outputs) - 1
        new_outpoints = []
        for i, o in enumerate(outputs):
            val = o.value
            if i == last_ouput_idx and val not in PS_DENOMS_VALS:  # change
                continue
            new_outpoint = f'{txid}:{i}'
            if i == 0 and val == CREATE_COLLATERAL_VAL:  # collaterral
                new_outpoints.append((new_outpoint, o.address, val))
            elif val in PS_DENOMS_VALS:  # denom round 0
                new_outpoints.append((new_outpoint, o.address, val))
        with self.denoms_lock, self.collateral_lock:
            for new_outpoint, addr, val in new_outpoints:
                if val == CREATE_COLLATERAL_VAL:  # collaterral
                    new_collateral = (addr, val)
                    w.db.add_ps_collateral(new_outpoint, new_collateral)
                else:  # denom round 0
                    new_denom = (addr, val, 0)
                    self.add_ps_denom(new_outpoint, new_denom)

    def _rm_new_denoms_ps_data(self, txid, tx):
        w = self.wallet
        self._rm_spent_ps_outpoints_ps_data(txid, tx)
        outputs = tx.outputs()
        last_ouput_idx = len(outputs) - 1
        rm_outpoints = []
        for i, o in enumerate(outputs):
            val = o.value
            if i == last_ouput_idx and val not in PS_DENOMS_VALS:  # change
                continue
            rm_outpoint = f'{txid}:{i}'
            if i == 0 and val == CREATE_COLLATERAL_VAL:  # collaterral
                rm_outpoints.append((rm_outpoint, val))
            elif val in PS_DENOMS_VALS:  # denom
                rm_outpoints.append((rm_outpoint, val))
        with self.denoms_lock, self.collateral_lock:
            for rm_outpoint, val in rm_outpoints:
                if val == CREATE_COLLATERAL_VAL:  # collaterral
                    w.db.pop_ps_collateral(rm_outpoint)
                else:  # denom round 0
                    self.pop_ps_denom(rm_outpoint)

    @unpack_io_values
    def _check_new_collateral_tx_err(self, txid, io_values, full_check):
        (inputs, outputs,
         icnt, mine_icnt, others_icnt, ocnt, op_return_ocnt) = io_values
        if others_icnt > 0:
            return 'Transaction has not mine inputs'
        if op_return_ocnt > 0:
            return 'Transaction has OP_RETURN outputs'
        if mine_icnt == 0:
            return 'Transaction has not enough inputs count'

        i_first = inputs[0][0]
        o_last = outputs[-1][0]
        if ocnt == 2:
            if o_last.address != i_first.address:  # check it is change output
                return 'Transaction has wrong change address'
            o_first = outputs[0][0]
            if o_first.value != CREATE_COLLATERAL_VAL:
                return 'Transaction has wrong output value'
        elif ocnt == 1:
            if o_last.value != CREATE_COLLATERAL_VAL:  # maybe no change
                return 'Transaction has wrong output value'
        else:
            return 'Transaction has wrong outputs count'

    def _add_new_collateral_ps_data(self, txid, tx):
        w = self.wallet
        self._add_spent_ps_outpoints_ps_data(txid, tx)
        out0 = tx.outputs()[0]
        addr = out0.address
        val = out0.value
        new_outpoint = f'{txid}:{0}'
        with self.collateral_lock:
            if val == CREATE_COLLATERAL_VAL:  # collaterral
                new_collateral = (addr, val)
                w.db.add_ps_collateral(new_outpoint, new_collateral)

    def _rm_new_collateral_ps_data(self, txid, tx):
        w = self.wallet
        self._rm_spent_ps_outpoints_ps_data(txid, tx)
        out0 = tx.outputs()[0]
        val = out0.value
        rm_outpoint = f'{txid}:{0}'
        with self.collateral_lock:
            if val == CREATE_COLLATERAL_VAL:  # collaterral
                w.db.pop_ps_collateral(rm_outpoint)

    @unpack_io_values
    def _check_pay_collateral_tx_err(self, txid, io_values, full_check):
        (inputs, outputs,
         icnt, mine_icnt, others_icnt, ocnt, op_return_ocnt) = io_values
        if others_icnt > 0:
            return 'Transaction has not mine inputs'
        if mine_icnt != 1:
            return 'Transaction has wrong inputs count'
        if ocnt != 1:
            return 'Transaction has wrong outputs count'

        i, i_prev_h, i_prev_n, is_mine = inputs[0]
        if i.value not in [COLLATERAL_VAL*4,  COLLATERAL_VAL*3,
                           COLLATERAL_VAL*2, COLLATERAL_VAL]:
            return 'Wrong collateral amount'

        o, o_prev_h, o_prev_n = outputs[0]
        if o.address.lower() == '6a':
            if o.value != 0:
                return 'Wrong output collateral amount'
        else:
            if o.value not in [COLLATERAL_VAL*3,  COLLATERAL_VAL*2,
                               COLLATERAL_VAL]:
                return 'Wrong output collateral amount'

        if not full_check:
            return

        w = self.wallet
        if not self.ps_collateral_cnt:
            return 'Collateral amount not ready'
        outpoint = f'{i_prev_h}:{i_prev_n}'
        ps_collateral = w.db.get_ps_collateral(outpoint)
        if not ps_collateral:
            return 'Collateral amount not found'

    def _add_pay_collateral_ps_data(self, txid, tx):
        w = self.wallet
        in0 = tx.inputs()[0]
        spent_prev_h = in0['prevout_hash']
        spent_prev_n = in0['prevout_n']
        spent_outpoint = f'{spent_prev_h}:{spent_prev_n}'
        spent_ps_addrs = set()
        with self.collateral_lock:
            spent_collateral = w.db.get_ps_spent_collateral(spent_outpoint)
            if not spent_collateral:
                spent_collateral = w.db.get_ps_collateral(spent_outpoint)
                if not spent_collateral:
                    raise AddPSDataError(f'ps_collateral {spent_outpoint}'
                                         f' not found')
            w.db.add_ps_spent_collateral(spent_outpoint, spent_collateral)
            spent_ps_addrs.add(spent_collateral[0])
            w.db.pop_ps_collateral(spent_outpoint)
            self.add_spent_addrs(spent_ps_addrs)

            out0 = tx.outputs()[0]
            addr = out0.address
            if addr.lower() != '6a':
                new_outpoint = f'{txid}:{0}'
                new_collateral = (addr, out0.value)
                w.db.add_ps_collateral(new_outpoint, new_collateral)
                w.db.pop_ps_reserved(addr)
                # add change address to not wait on wallet.synchronize_sequence
                if hasattr(w, '_unused_change_addresses'):
                    # _unused_change_addresses absent on wallet startup and
                    # wallet.create_new_address fails in that case
                    limit = w.gap_limit_for_change
                    addrs = w.get_change_addresses()
                    last_few_addrs = addrs[-limit:]
                    if any(map(w.db.get_addr_history, last_few_addrs)):
                        w.create_new_address(for_change=True)

    def _rm_pay_collateral_ps_data(self, txid, tx):
        w = self.wallet
        in0 = tx.inputs()[0]
        restore_prev_h = in0['prevout_hash']
        restore_prev_n = in0['prevout_n']
        restore_outpoint = f'{restore_prev_h}:{restore_prev_n}'
        restored_ps_addrs = set()
        with self.collateral_lock:
            tx_type, completed = w.db.get_ps_tx_removed(restore_prev_h)
            if not tx_type:
                restore_collateral = w.db.get_ps_collateral(restore_outpoint)
                if not restore_collateral:
                    restore_collateral = \
                        w.db.get_ps_spent_collateral(restore_outpoint)
                    if not restore_collateral:
                        raise RmPSDataError(f'ps_spent_collateral'
                                            f' {restore_outpoint} not found')
                w.db.add_ps_collateral(restore_outpoint, restore_collateral)
                restored_ps_addrs.add(restore_collateral[0])
            w.db.pop_ps_spent_collateral(restore_outpoint)
            self.restore_spent_addrs(restored_ps_addrs)

            out0 = tx.outputs()[0]
            addr = out0.address
            if addr.lower() != '6a':
                rm_outpoint = f'{txid}:{0}'
                w.db.add_ps_reserved(addr, restore_outpoint)
                w.db.pop_ps_collateral(rm_outpoint)

    @unpack_io_values
    def _check_denominate_tx_err(self, txid, io_values, full_check):
        (inputs, outputs,
         icnt, mine_icnt, others_icnt, ocnt, op_return_ocnt) = io_values
        if icnt != ocnt:
            return 'Transaction has different count of inputs/outputs'
        if icnt < POOL_MIN_PARTICIPANTS:
            return 'Transaction has too small count of inputs/outputs'
        if icnt > POOL_MAX_PARTICIPANTS * PRIVATESEND_ENTRY_MAX_SIZE:
            return 'Transaction has too many count of inputs/outputs'
        if mine_icnt < 1:
            return 'Transaction has too small count of mine inputs'
        if op_return_ocnt > 0:
            return 'Transaction has OP_RETURN outputs'

        denom_val = None
        for i, prev_h, prev_n, is_mine, in inputs:
            if not is_mine:
                continue
            if denom_val is None:
                denom_val = i.value
                if denom_val not in PS_DENOMS_VALS:
                    return f'Unsuitable input value={denom_val}'
            elif i.value != denom_val:
                return f'Unsuitable input value={i.value}'
        for o, prev_h, prev_n in outputs:
            if o.value != denom_val:
                return f'Unsuitable output value={o.value}'

        if not full_check:
            return

        w = self.wallet
        for i, prev_h, prev_n, is_mine, in inputs:
            if not is_mine:
                continue
            denom = w.db.get_ps_denom(f'{prev_h}:{prev_n}')
            if not denom:
                return f'Transaction input not found in ps_denoms'

    def _check_denominate_tx_io_on_wfl(self, txid, tx, wfl):
        w = self.wallet
        icnt = 0
        ocnt = 0
        for i, txin in enumerate(tx.inputs()):
            txin = copy.deepcopy(txin)
            w.add_input_info(txin)
            addr = txin['address']
            if not w.is_mine(addr):
                continue
            prev_h = txin['prevout_hash']
            prev_n = txin['prevout_n']
            outpoint = f'{prev_h}:{prev_n}'
            if outpoint in wfl.inputs:
                icnt += 1
        for i, o in enumerate(tx.outputs()):
            if o.value != wfl.denom:
                return False
            if o.address in wfl.outputs:
                ocnt += 1
        if icnt > 0 and ocnt == icnt:
            return True
        else:
            return False

    def _is_mine_slow(self, addr, for_change=False, look_ahead_cnt=100):
        # need look_ahead_cnt is max 16 sessions * avg 5 addresses is ~ 80
        w = self.wallet
        if w.is_mine(addr):
            return True
        if self.state in self.mixing_running_states:
            return False

        if for_change:
            last_wallet_addr = w.db.get_change_addresses(slice_start=-1)[0]
            last_wallet_index = w.get_address_index(last_wallet_addr)[1]
        else:
            last_wallet_addr = w.db.get_receiving_addresses(slice_start=-1)[0]
            last_wallet_index = w.get_address_index(last_wallet_addr)[1]

        # prepare cache
        cache = getattr(self, '_is_mine_slow_cache', {})
        if not cache:
            cache['change'] = {}
            cache['recv'] = {}
            self._is_mine_slow_cache = cache

        cache_type = 'change' if for_change else 'recv'
        cache = cache[cache_type]
        if 'first_idx' not in cache:
            cache['addrs'] = addrs = list()
            cache['first_idx'] = first_idx = last_wallet_index + 1
        else:
            addrs = cache['addrs']
            first_idx = cache['first_idx']
            if addr in addrs:
                return True
            elif first_idx < last_wallet_index + 1:
                difference = last_wallet_index + 1 - first_idx
                cache['addrs'] = addrs = addrs[difference:]
                cache['first_idx'] = first_idx = last_wallet_index + 1

        # generate new addrs and check match
        idx = first_idx + len(addrs)
        while len(addrs) < look_ahead_cnt:
            sequence = [1, idx] if for_change else [0, idx]
            x_pubkey = w.keystore.get_xpubkey(*sequence)
            _, generated_addr = xpubkey_to_address(x_pubkey)
            if generated_addr not in addrs:
                addrs.append(generated_addr)
            if addr in addrs:
                return True
            idx += 1

        if w.is_mine(addr):
            return True
        else:
            return False

    def _add_denominate_ps_data(self, txid, tx):
        w = self.wallet
        spent_outpoints = []
        for txin in tx.inputs():
            txin = copy.deepcopy(txin)
            w.add_input_info(txin)
            addr = txin['address']
            if not w.is_mine(addr):
                continue
            spent_prev_h = txin['prevout_hash']
            spent_prev_n = txin['prevout_n']
            spent_outpoint = f'{spent_prev_h}:{spent_prev_n}'
            spent_outpoints.append(spent_outpoint)

        new_outpoints = []
        for i, o in enumerate(tx.outputs()):
            addr = o.address
            if not self._is_mine_slow(addr):
                continue
            new_outpoints.append((f'{txid}:{i}', addr, o.value))

        input_rounds = []
        spent_ps_addrs = set()
        with self.denoms_lock:
            for spent_outpoint in spent_outpoints:
                spent_denom = w.db.get_ps_spent_denom(spent_outpoint)
                if not spent_denom:
                    spent_denom = w.db.get_ps_denom(spent_outpoint)
                    if not spent_denom:
                        raise AddPSDataError(f'ps_denom {spent_outpoint}'
                                             f' not found')
                w.db.add_ps_spent_denom(spent_outpoint, spent_denom)
                spent_ps_addrs.add(spent_denom[0])
                self.pop_ps_denom(spent_outpoint)
                input_rounds.append(spent_denom[2])
            self.add_spent_addrs(spent_ps_addrs)

            random.shuffle(input_rounds)
            for i, (new_outpoint, addr, value) in enumerate(new_outpoints):
                new_denom = (addr, value, input_rounds[i]+1)
                self.add_ps_denom(new_outpoint, new_denom)
                w.db.pop_ps_reserved(addr)

    def _rm_denominate_ps_data(self, txid, tx):
        w = self.wallet
        restore_outpoints = []
        for txin in tx.inputs():
            txin = copy.deepcopy(txin)
            w.add_input_info(txin)
            addr = txin['address']
            if not w.is_mine(addr):
                continue
            restore_prev_h = txin['prevout_hash']
            restore_prev_n = txin['prevout_n']
            restore_outpoint = f'{restore_prev_h}:{restore_prev_n}'
            restore_outpoints.append((restore_outpoint, restore_prev_h))

        rm_outpoints = []
        for i, o in enumerate(tx.outputs()):
            addr = o.address
            if not self._is_mine_slow(addr):
                continue
            rm_outpoints.append((f'{txid}:{i}', addr))

        restored_ps_addrs = set()
        with self.denoms_lock:
            for restore_outpoint, restore_prev_h in restore_outpoints:
                tx_type, completed = w.db.get_ps_tx_removed(restore_prev_h)
                if not tx_type:
                    restore_denom = w.db.get_ps_denom(restore_outpoint)
                    if not restore_denom:
                        restore_denom = \
                            w.db.get_ps_spent_denom(restore_outpoint)
                        if not restore_denom:
                            raise RmPSDataError(f'ps_denom {restore_outpoint}'
                                                f' not found')
                    self.add_ps_denom(restore_outpoint, restore_denom)
                    restored_ps_addrs.add(restore_denom[0])
                w.db.pop_ps_spent_denom(restore_outpoint)
            self.restore_spent_addrs(restored_ps_addrs)

            for i, (rm_outpoint, addr) in enumerate(rm_outpoints):
                w.db.add_ps_reserved(addr, restore_outpoints[i][0])
                self.pop_ps_denom(rm_outpoint)

    @unpack_io_values
    def _check_other_ps_coins_tx_err(self, txid, io_values, full_check):
        (inputs, outputs,
         icnt, mine_icnt, others_icnt, ocnt, op_return_ocnt) = io_values

        w = self.wallet
        for o, prev_h, prev_n in outputs:
            addr = o.address
            if addr in w.db.get_ps_addresses():
                return
        return 'Transaction has no outputs with ps denoms/collateral addresses'

    @unpack_io_values
    def _check_privatesend_tx_err(self, txid, io_values, full_check):
        (inputs, outputs,
         icnt, mine_icnt, others_icnt, ocnt, op_return_ocnt) = io_values
        if others_icnt > 0:
            return 'Transaction has not mine inputs'
        if mine_icnt < 1:
            return 'Transaction has too small count of mine inputs'
        if op_return_ocnt > 0:
            return 'Transaction has OP_RETURN outputs'
        if ocnt != 1:
            return 'Transaction has wrong count of outputs'

        w = self.wallet
        for i, prev_h, prev_n, is_mine in inputs:
            if i.value not in PS_DENOMS_VALS:
                return f'Unsuitable input value={i.value}'
            denom = w.db.get_ps_denom(f'{prev_h}:{prev_n}')
            if not denom:
                return f'Transaction input not found in ps_denoms'
            if denom[2] < self.min_mix_rounds:
                return f'Transaction input mix_rounds too small'

    @unpack_io_values
    def _check_spend_ps_coins_tx_err(self, txid, io_values, full_check):
        (inputs, outputs,
         icnt, mine_icnt, others_icnt, ocnt, op_return_ocnt) = io_values
        if others_icnt > 0:
            return 'Transaction has not mine inputs'
        if mine_icnt == 0:
            return 'Transaction has not enough inputs count'

        w = self.wallet
        for i, prev_h, prev_n, is_mine in inputs:
            spent_outpoint = f'{prev_h}:{prev_n}'
            if w.db.get_ps_denom(spent_outpoint):
                return
            if w.db.get_ps_collateral(spent_outpoint):
                return
            if w.db.get_ps_other(spent_outpoint):
                return
        return 'Transaction has no inputs from ps denoms/collaterals/others'

    def _add_spend_ps_coins_ps_data(self, txid, tx):
        w = self.wallet
        self._add_spent_ps_outpoints_ps_data(txid, tx)
        ps_addrs = w.db.get_ps_addresses()
        new_others = []
        for i, o in enumerate(tx.outputs()):  # check to add ps_others
            addr = o.address
            if addr in ps_addrs:
                new_others.append((f'{txid}:{i}', addr, o.value))
        with self.others_lock:
            for new_outpoint, addr, value in new_others:
                new_other = (addr, value)
                w.db.add_ps_other(new_outpoint, new_other)

    def _rm_spend_ps_coins_ps_data(self, txid, tx):
        w = self.wallet
        self._rm_spent_ps_outpoints_ps_data(txid, tx)
        ps_addrs = w.db.get_ps_addresses()
        rm_others = []
        for i, o in enumerate(tx.outputs()):  # check to rm ps_others
            addr = o.address
            if addr in ps_addrs:
                rm_others.append(f'{txid}:{i}')
        with self.others_lock:
            for rm_outpoint in rm_others:
                w.db.pop_ps_other(rm_outpoint)

    # Methods to add ps data, using preceding methods for different tx types
    def _check_ps_tx_type(self, txid, tx,
                          find_untracked=False, last_iteration=False):
        if find_untracked and last_iteration:
            err = self._check_other_ps_coins_tx_err(txid, tx)
            if not err:
                return PSTxTypes.OTHER_PS_COINS
            else:
                return STANDARD_TX

        if self._check_on_denominate_wfl(txid, tx):
            return PSTxTypes.DENOMINATE
        if self._check_on_pay_collateral_wfl(txid, tx):
            return PSTxTypes.PAY_COLLATERAL
        if self._check_on_new_collateral_wfl(txid, tx):
            return PSTxTypes.NEW_COLLATERAL
        if self._check_on_new_denoms_wfl(txid, tx):
            return PSTxTypes.NEW_DENOMS

        # OTHER_PS_COINS before PRIVATESEND and SPEND_PS_COINS
        # to prevent spending ps coins to ps addresses
        # Do not must happen if blocked in PSManager.broadcast_transaction
        err = self._check_other_ps_coins_tx_err(txid, tx)
        if not err:
            return PSTxTypes.OTHER_PS_COINS
        # PRIVATESEND before SPEND_PS_COINS as second pattern more relaxed
        err = self._check_privatesend_tx_err(txid, tx)
        if not err:
            return PSTxTypes.PRIVATESEND
        # SPEND_PS_COINS will be allowed when mixing is stopped
        err = self._check_spend_ps_coins_tx_err(txid, tx)
        if not err:
            return PSTxTypes.SPEND_PS_COINS

        return STANDARD_TX

    def _add_ps_data(self, txid, tx, tx_type):
        w = self.wallet
        w.db.add_ps_tx(txid, tx_type, completed=False)
        if tx_type == PSTxTypes.NEW_DENOMS:
            self._add_new_denoms_ps_data(txid, tx)
            if self._keypairs_cache:
                self._cleanup_spendable_keypairs(txid, tx, tx_type)
        elif tx_type == PSTxTypes.NEW_COLLATERAL:
            self._add_new_collateral_ps_data(txid, tx)
            if self._keypairs_cache:
                self._cleanup_spendable_keypairs(txid, tx, tx_type)
        elif tx_type == PSTxTypes.PAY_COLLATERAL:
            self._add_pay_collateral_ps_data(txid, tx)
            self._process_by_pay_collateral_wfl(txid, tx)
            if self._keypairs_cache:
                self._cleanup_ps_keypairs(txid, tx, tx_type)
        elif tx_type == PSTxTypes.DENOMINATE:
            self._add_denominate_ps_data(txid, tx)
            self._process_by_denominate_wfl(txid, tx)
            if self._keypairs_cache:
                self._cleanup_ps_keypairs(txid, tx, tx_type)
        elif tx_type == PSTxTypes.PRIVATESEND:
            self._add_spend_ps_coins_ps_data(txid, tx)
            if self._keypairs_cache:
                self._cleanup_ps_keypairs(txid, tx, tx_type)
        elif tx_type == PSTxTypes.SPEND_PS_COINS:
            self._add_spend_ps_coins_ps_data(txid, tx)
            if self._keypairs_cache:
                self._cleanup_ps_keypairs(txid, tx, tx_type)
        elif tx_type == PSTxTypes.OTHER_PS_COINS:
            self._add_spend_ps_coins_ps_data(txid, tx)
            if self._keypairs_cache:
                self._cleanup_ps_keypairs(txid, tx, tx_type)
        else:
            raise AddPSDataError(f'{txid} unknow type {tx_type}')
        w.db.pop_ps_tx_removed(txid)
        w.db.add_ps_tx(txid, tx_type, completed=True)

    def _add_tx_ps_data(self, txid, tx):
        '''Used from AddressSynchronizer.add_transaction'''
        if self.state != PSStates.Mixing:
            return
        w = self.wallet
        tx_type, completed = w.db.get_ps_tx(txid)
        if tx_type and completed:  # ps data already exists
            return
        if not tx_type:  # try to find type in removed ps txs
            tx_type, completed = w.db.get_ps_tx_removed(txid)
            if tx_type:
                self.logger.info(f'_add_tx_ps_data: matched removed tx {txid}')
        if not tx_type:  # check possible types from workflows and patterns
            tx_type = self._check_ps_tx_type(txid, tx)
        if not tx_type:
            return
        self._add_tx_type_ps_data(txid, tx, tx_type)

    def _add_tx_type_ps_data(self, txid, tx, tx_type):
        w = self.wallet
        if tx_type in PS_SAVED_TX_TYPES:
            try:
                type_name = SPEC_TX_NAMES[tx_type]
                self._add_ps_data(txid, tx, tx_type)
                self.last_mixed_tx_time = time.time()
                self.logger.debug(f'_add_tx_type_ps_data {txid}, {type_name}')
                self.postpone_notification('ps-data-changes', w)
            except Exception as e:
                self.logger.info(f'_add_ps_data {txid} failed: {str(e)}')
                if tx_type in [PSTxTypes.NEW_COLLATERAL, PSTxTypes.NEW_DENOMS]:
                    # this two tx types added during wfl creation process
                    raise
                if tx_type in [PSTxTypes.PAY_COLLATERAL, PSTxTypes.DENOMINATE]:
                    # this two tx types added from network
                    msg = self.ADD_PS_DATA_ERR_MSG
                    msg = f'{msg} {type_name} {txid}:\n{str(e)}'
                    self.stop_mixing(msg)
        else:
            self.logger.info(f'_add_tx_type_ps_data: {txid}'
                             f' unknonw type {tx_type}')

    # Methods to rm ps data, using preceding methods for different tx types
    def _rm_ps_data(self, txid, tx, tx_type):
        w = self.wallet
        w.db.add_ps_tx_removed(txid, tx_type, completed=False)
        if tx_type == PSTxTypes.NEW_DENOMS:
            self._rm_new_denoms_ps_data(txid, tx)
            self._cleanup_new_denoms_wfl_tx_data(txid)
        elif tx_type == PSTxTypes.NEW_COLLATERAL:
            self._rm_new_collateral_ps_data(txid, tx)
            self._cleanup_new_collateral_wfl_tx_data(txid)
        elif tx_type == PSTxTypes.PAY_COLLATERAL:
            self._rm_pay_collateral_ps_data(txid, tx)
            self._cleanup_pay_collateral_wfl_tx_data(txid)
        elif tx_type == PSTxTypes.DENOMINATE:
            self._rm_denominate_ps_data(txid, tx)
        elif tx_type == PSTxTypes.PRIVATESEND:
            self._rm_spend_ps_coins_ps_data(txid, tx)
        elif tx_type == PSTxTypes.SPEND_PS_COINS:
            self._rm_spend_ps_coins_ps_data(txid, tx)
        elif tx_type == PSTxTypes.OTHER_PS_COINS:
            self._rm_spend_ps_coins_ps_data(txid, tx)
        else:
            raise RmPSDataError(f'{txid} unknow type {tx_type}')
        w.db.pop_ps_tx(txid)
        w.db.add_ps_tx_removed(txid, tx_type, completed=True)

    def _rm_tx_ps_data(self, txid):
        '''Used from AddressSynchronizer.remove_transaction'''
        w = self.wallet
        tx = w.db.get_transaction(txid)
        if not tx:
            self.logger.info(f'_rm_tx_ps_data: {txid} not found')
            return

        tx_type, completed = w.db.get_ps_tx(txid)
        if not tx_type:
            return
        if tx_type in PS_SAVED_TX_TYPES:
            try:
                self._rm_ps_data(txid, tx, tx_type)
                self.postpone_notification('ps-data-changes', w)
            except Exception as e:
                self.logger.info(f'_rm_ps_data {txid} failed: {str(e)}')
        else:
            self.logger.info(f'_rm_tx_ps_data: {txid} unknonw type {tx_type}')

    # Auxiliary methods
    def clear_ps_data(self):
        w = self.wallet
        msg = None
        with self.state_lock:
            if self.state in self.mixing_running_states:
                msg = _('To clear PrivateSend data stop PrivateSend mixing')
            elif self.state == PSStates.FindingUntracked:
                msg = _('Can not clear PrivateSend data. Process of finding'
                        ' untracked PS transactions is currently run')
            else:
                self.logger.info(f'Clearing PrivateSend wallet data')
                w.db.clear_ps_data()
                self.state == PSStates.Initializing
                self.logger.info(f'All PrivateSend wallet data cleared')
        if msg:
            self.trigger_callback('ps-state-changes', w, msg, None)
        else:
            self.trigger_callback('ps-state-changes', w, None, None)
            self.postpone_notification('ps-data-changes', w)
            w.storage.write()

    def find_untracked_ps_txs_from_gui(self):
        if self.loop:
            coro = self.find_untracked_ps_txs()
            asyncio.run_coroutine_threadsafe(coro, self.loop)

    async def find_untracked_ps_txs(self, log=True):
        w = self.wallet
        msg = None
        found = 0
        with self.state_lock:
            if self.state in [PSStates.Ready, PSStates.Initializing]:
                self.state = PSStates.FindingUntracked
        if not self.state == PSStates.FindingUntracked:
            return found
        else:
            self.trigger_callback('ps-state-changes', w, None, None)
        try:
            _find = self._find_untracked_ps_txs
            found = await self.loop.run_in_executor(None, _find, log)
            if found:
                w.storage.write()
                self.postpone_notification('ps-data-changes', w)
        except Exception as e:
            with self.state_lock:
                self.state = PSStates.Errored
            self.logger.info(f'Error during loading of untracked'
                             f' PS transactions: {str(e)}')
        finally:
            _find_uncompleted = self._fix_uncompleted_ps_txs
            await self.loop.run_in_executor(None, _find_uncompleted)
            with self.state_lock:
                if self.state != PSStates.Errored:
                    self.state = PSStates.Ready
            self.trigger_callback('ps-state-changes', w, None, None)
        return found

    def _fix_uncompleted_ps_txs(self):
        w = self.wallet
        ps_txs = w.db.get_ps_txs()
        ps_txs_removed = w.db.get_ps_txs_removed()
        found = 0
        failed = 0
        for txid, (tx_type, completed) in ps_txs.items():
            if completed:
                continue
            tx = w.db.get_transaction(txid)
            if tx:
                try:
                    self.logger.info(f'_fix_uncompleted_ps_txs:'
                                     f' add {txid} ps data')
                    self._add_ps_data(txid, tx, tx_type)
                    found += 1
                except Exception as e:
                    str_err = f'_add_ps_data {txid} failed: {str(e)}'
                    failed += 1
                    self.logger.info(str_err)
        for txid, (tx_type, completed) in ps_txs_removed.items():
            if completed:
                continue
            tx = w.db.get_transaction(txid)
            if tx:
                try:
                    self.logger.info(f'_fix_uncompleted_ps_txs:'
                                     f' rm {txid} ps data')
                    self._rm_ps_data(txid, tx, tx_type)
                    found += 1
                except Exception as e:
                    str_err = f'_rm_ps_data {txid} failed: {str(e)}'
                    failed += 1
                    self.logger.info(str_err)
        if failed != 0:
            with self.state_lock:
                self.state == PSStates.Errored
        if found:
            self.postpone_notification('ps-data-changes', w)

    def _get_simplified_history(self):
        w = self.wallet
        history = []
        for txid in w.db.list_transactions():
            tx = w.db.get_transaction(txid)
            tx_type, completed = w.db.get_ps_tx(txid)
            islock = w.db.get_islock(txid)
            if islock:
                tx_mined_status = w.get_tx_height(txid)
                islock_sort = txid if not tx_mined_status.conf else ''
            else:
                islock_sort = ''
            history.append((txid, tx, tx_type, islock, islock_sort))
        history.sort(key=lambda x: (w.get_txpos(x[0], x[3]), x[4]))
        return history

    @profiler
    def _find_untracked_ps_txs(self, log):
        w = self.wallet
        if log:
            self.logger.info(f'Finding untracked PrivateSend transactions')
        history = self._get_simplified_history()
        all_detected_txs = set()
        found = 0
        while True:
            detected_txs = set()
            not_detected_parents = set()
            for txid, tx, tx_type, islock, islock_sort in history:
                if tx_type or txid in all_detected_txs:  # already found
                    continue
                tx_type = self._check_ps_tx_type(txid, tx, find_untracked=True)
                if tx_type:
                    self._add_ps_data(txid, tx, tx_type)
                    type_name = SPEC_TX_NAMES[tx_type]
                    if log:
                        self.logger.info(f'Found {type_name} {txid}')
                    found += 1
                    detected_txs.add(txid)
                else:
                    parents = set([i['prevout_hash'] for i in tx.inputs()])
                    not_detected_parents |= parents
            all_detected_txs |= detected_txs
            if not detected_txs & not_detected_parents:
                break
        # last iteration to detect PS Other Coins not found before other ps txs
        for txid, tx, tx_type, islock, islock_sort in history:
            if tx_type or txid in all_detected_txs:  # already found
                continue
            tx_type = self._check_ps_tx_type(txid, tx, find_untracked=True,
                                             last_iteration=True)
            if tx_type:
                self._add_ps_data(txid, tx, tx_type)
                type_name = SPEC_TX_NAMES[tx_type]
                if log:
                    self.logger.info(f'Found {type_name} {txid}')
                found += 1
        if not found and log:
            self.logger.info(f'No untracked PrivateSend'
                             f' transactions found')
        return found

    def find_common_ancestor(self, utxo_a, utxo_b, search_depth=5):
        w = self.wallet
        min_common_depth = 1e9
        cur_depth = 0
        cur_utxos_a = [(utxo_a, ())]
        cur_utxos_b = [(utxo_b, ())]
        txids_a = {}
        txids_b = {}
        while cur_depth <= search_depth:
            next_utxos_a = []
            for utxo, path in cur_utxos_a:
                txid = utxo['prevout_hash']
                txid_path = path + (txid, )
                txids_a[txid] = txid_path
                tx = w.db.get_transaction(txid)
                if tx:
                    for txin in tx.inputs():
                        txin = copy.deepcopy(txin)
                        w.add_input_info(txin)
                        addr = txin['address']
                        if addr and w.is_mine(addr):
                            next_utxos_a.append((txin, txid_path))
            cur_utxos_a = next_utxos_a[:]

            next_utxos_b = []
            for utxo, path in cur_utxos_b:
                txid = utxo['prevout_hash']
                txid_path = path + (txid, )
                txids_b[txid] = txid_path
                tx = w.db.get_transaction(txid)
                if tx:
                    for txin in tx.inputs():
                        txin = copy.deepcopy(txin)
                        w.add_input_info(txin)
                        addr = txin['address']
                        if addr and w.is_mine(addr):
                            next_utxos_b.append((txin, txid_path))
            cur_utxos_b = next_utxos_b[:]

            common_txids = set(txids_a).intersection(txids_b)
            if common_txids:
                res = {'paths_a': [], 'paths_b': []}
                for txid in common_txids:
                    path_a = txids_a[txid]
                    path_b = txids_b[txid]
                    min_common_depth = min(min_common_depth, len(path_a) - 1)
                    min_common_depth = min(min_common_depth, len(path_b) - 1)
                    res['paths_a'].append(path_a)
                    res['paths_b'].append(path_b)
                res['min_common_depth'] = min_common_depth
                return res

            cur_utxos_a = next_utxos_a[:]
            cur_depth += 1
