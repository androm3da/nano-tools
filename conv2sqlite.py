#!/usr/bin/env python3
import sys, collections, os, time
from struct import unpack
import click
import lmdb, apsw
import progressbar

from rainumbers import *
from nanodb import KNOWN_ACCOUNTS, GENESIS_OPEN_BLOCK_HASH, GENESIS_ACCOUNT, GENESIS_PUBLIC_KEY
from toposort import topological_sort, generate_block_dependencies

DATADIR = 'RaiBlocks'
DBPREFIX = 'data.ldb'
RAIBLOCKS_LMDB_DB = os.path.join(os.environ['HOME'], DATADIR, DBPREFIX)

DEFAULT_SQLITE_DB = 'nano.db'

# XXX store signature and work in separate table
SCHEMA = """
begin;

drop table if exists accounts;
drop table if exists blocks;
drop table if exists block_info;
drop table if exists block_validation;

create table accounts
(
    id          integer not null,
    address     text not null,      -- xrb_....

    name        text,

    -- could store derived quantities, like
    -- number of blocks
    -- balance

    primary key(id),
    unique(address)
);

create table blocks
(
    id          integer not null,
    hash        text not null,
    type        text not null,

    -- All blocks
    previous    integer,            -- [block]      Can be NULL for an open block
    next        integer,            -- [block]      "successor" in LMDB; called "next" to match "previous"
    
    -- Depending on block type
    work            text,           --              change, open, receive, send
    representative  integer,        -- [account]    change
    source          integer,        -- [block]      open, receive
    destination     integer,        -- [account]    send
    balance         float,          --              Mxrb, float representation (not fully precise, 8 byte precision instead of needed 16 bytes)
    balance_raw     text,           --              raw, integer represented as string
    account         integer,        -- [account]    open, vote
    --sequence_number integer,        -- vote
    --block           text,           -- vote

    primary key(id),
    unique(hash)
);

create table block_validation
(
    id          integer not null,
    
    signature   text not null,
    work        text,
    
    primary key(id)
);

create table block_info
(
    block           integer not null,
    account         integer not null,
    
    chain_index     integer not null,   -- Index in account chain (0 = open block)    
    global_index    integer,            -- Index in the global topological sort (0 = genesis block)
    
    --balance   text,   -- balance at this block, in raw (string representation)
    --amount    text,   -- amount transfered by this block, in raw (string representation); only for send/receive/open blocks

    primary key(block)
);

commit;
"""

DROP_INDICES = """
drop index if exists accounts_address;

drop index if exists blocks_source;
drop index if exists blocks_destination;
drop index if exists blocks_account;
drop index if exists blocks_balance;
drop index if exists blocks_previous;
drop index if exists blocks_next;
drop index if exists blocks_type;

drop index if exists block_info_account;
drop index if exists block_info_chain_index;
drop index if exists block_info_global_index;
"""

CREATE_INDICES = """
create index accounts_address on accounts (address);

create index blocks_source on blocks (source);
create index blocks_destination on blocks (source);
create index blocks_account on blocks (account);
create index blocks_balance on blocks (balance);
create index blocks_previous on blocks (previous);
create index blocks_next on blocks (next);
create index blocks_type on blocks (type);

create index block_info_account on block_info (account);
create index block_info_chain_index on block_info (chain_index);
create index block_info_global_index on block_info (global_index);

analyze;
"""

# Map block hash (bytes) to integer ID
block_ids = {
    hex2bin(GENESIS_OPEN_BLOCK_HASH): 0
}

# Map account address ('xrb_...') to integer ID
account_ids = {
    GENESIS_ACCOUNT: 0
}

next_block_id = 1
next_account_id = 1

def get_block_id(blockhash):
    # XXX this takes a bytes object, while get_account_id takes a string :-/

    if bin2hex(blockhash) == '0000000000000000000000000000000000000000000000000000000000000000':
        # Used in the LMDB database to indicate a null block "pointer"
        return None
        
    if bin2hex(blockhash) == GENESIS_PUBLIC_KEY:
        # Source block of the genesis open block does not exist
        return None

    global next_block_id
    try:
        return block_ids[blockhash]
    except KeyError:
        block_ids[blockhash] = next_block_id
        next_block_id += 1
        return next_block_id-1

def get_account_id(address):
    assert address.startswith('xrb_') and len(address) == 64

    global next_account_id
    try:
        return account_ids[address]
    except KeyError:
        account_ids[address] = next_account_id
        next_account_id += 1
        return next_account_id-1


"""
def process_vote_entry(sqlcur, key, value):

    # secure.cpp, vote::serialize()

    account = value[:32]
    signature = value[32:96]
    sequence_number = unpack('<Q', value[96:104])[0]
    successor = value[152:184]
    #assert len(value[184:]) == 0

    print('Vote block %s' % bin2hex(key))
    print('... voting account %s (%s)' % (bin2hex(account), encode_account(account)))
    print('... signature %s' % bin2hex(signature))
    print('... sequence_number %08x' % sequence_number)
    print('... block %s' % bin2hex(value[104:]))

    sqlcur.execute('insert into blocks (hash, type, account, signature, sequence_number, block) values (?,?,?,?,?,?)',
        (bin2hex(key), 'vote', encode_account(account), bin2hex(signature), sequence_number, bin2hex(value[104:])))
"""

def process_open_entry(sqlcur, key, value):

    # blocks.cpp, deserialize_block(stream, type), rai::open_block members

    """
    Special case: open block of the genesis account (991CF190094C00F0B68E2E5F75F6BEE95A2E0BD93CEAA4A6734DB9F19B728948):
        
    {
    "type": "open",
    "source": "E89208DD038FBB269987689621D52292AE9C35941A7484756ECCED92A65093BA",
    "representative": "xrb_3t6k35gi95xu6tergt6p69ck76ogmitsa8mnijtpxm9fkcm736xtoncuohr3",
    "account": "xrb_3t6k35gi95xu6tergt6p69ck76ogmitsa8mnijtpxm9fkcm736xtoncuohr3",
    "work": "62f05417dd3fb691",
    "signature": "9F0C933C8ADE004D808EA1985FA746A7E95BA2A38F867640F53EC8F180BDFE9E2C1268DEAD7C2664F356E37ABA362BC58E46DBA03E523A7B5A19E4B6EB12BB02"
    }
    
    See rai/secure.cpp for this block.
    
    The source block does not exist! 
    
    Receive 340,282,366.920938 XRB    
    
    Where does the genesis balance come from?
    Holy crap, from ledger_constants::genesis_amount() in rai/secure.cpp:
    
        genesis_amount (std::numeric_limits<rai::uint128_t>::max ())
        
    So, the initial amount available is 2**128-1 = 340282366920938463463374607431768211455 raw
    
    But why the non-existent source block? Ah, the source "block" is actually the
    public key of the Genesis account.
    
        char const * live_public_key_data = "E89208DD038FBB269987689621D52292AE9C35941A7484756ECCED92A65093BA"; // xrb_3t6k35gi95xu6tergt6p69ck76ogmitsa8mnijtpxm9fkcm736xtoncuohr3
    """

    source_block = value[:32]
    representative = value[32:64]
    account = value[64:96]
    signature = value[96:160]
    work = unpack('<Q', value[160:168])[0]
    successor = value[168:200]
    assert len(value[200:]) == 0

    """
    print('Open block %s' % bin2hex(key))
    print('... source block %s' % bin2hex(source_block))
    print('... representative %s (%s)' % (bin2hex(representative), encode_account(representative)))
    print('... account %s (%s)' % (bin2hex(account), encode_account(account)))
    print('... signature %s' % bin2hex(signature))
    print('... work %08x' % work)
    print('... successor %s' % bin2hex(successor))
    """

    block_id = get_block_id(key)

    hash = bin2hex(key)
    source_id = get_block_id(source_block)
    representative_id = get_account_id(encode_account(representative))
    account_id = get_account_id(encode_account(account))
    signature = bin2hex(signature)
    work = '%08x' % work
    successor_id = get_block_id(successor)

    sqlcur.execute('insert into blocks (id, hash, type, source, representative, account, next) values (?,?,?,?,?,?,?)',
        (block_id, hash, 'open', source_id, representative_id, account_id, successor_id))
        
    sqlcur.execute('insert into block_validation (id, signature, work) values (?,?,?)', 
        (block_id, signature, work))

def process_change_entry(sqlcur, key, value):

    # blocks.cpp, deserialize_block(stream, type), rai::change_block members

    previous_block = value[:32]
    representative = value[32:64]
    signature = value[64:128]
    work = unpack('<Q', value[128:136])[0]
    successor = value[136:168]
    assert len(value[168:]) == 0

    """
    print('Change block %s' % bin2hex(key))
    print('... previous block %s' % bin2hex(previous_block))
    print('... representative %s (%s)' % (bin2hex(representative), encode_account(representative)))
    print('... signature %s' % bin2hex(signature))
    print('... work %08x' % work)
    print('... successor %s' % bin2hex(successor))
    """

    block_id = get_block_id(key)

    hash = bin2hex(key)
    previous_id = get_block_id(previous_block)
    representative_id = get_account_id(encode_account(representative))
    signature = bin2hex(signature)
    work = '%08x' % work
    successor_id = get_block_id(successor)

    sqlcur.execute('insert into blocks (id, hash, type, previous, representative, next) values (?,?,?,?,?,?)',
        (block_id, hash, 'change', previous_id, representative_id, successor_id))
        
    sqlcur.execute('insert into block_validation (id, signature, work) values (?,?,?)', 
        (block_id, signature, work))

def process_receive_entry(sqlcur, key, value):

    # blocks.cpp, deserialize_block(stream, type), rai::receive_block members

    previous_block = value[:32]
    source_block = value[32:64]
    signature = value[64:128]
    work = unpack('<Q', value[128:136])[0]
    successor = value[136:168]
    assert len(value[168:]) == 0

    """
    print('Receive block %s' % bin2hex(key))
    print('... previous block %s' % bin2hex(previous_block))
    print('... source block %s' % bin2hex(source_block))
    print('... signature %s' % bin2hex(signature))
    print('... work %08x' % work)
    print('... successor %s' % bin2hex(successor))
    """

    block_id = get_block_id(key) # XXX _id

    hash = bin2hex(key)
    previous_id = get_block_id(previous_block)
    source_id = get_block_id(source_block)
    signature = bin2hex(signature)
    work = '%08x' % work
    successor_id = get_block_id(successor)

    sqlcur.execute('insert into blocks (id, hash, type, previous, source, next) values (?,?,?,?,?,?)',
        (block_id, hash, 'receive', previous_id, source_id, successor_id))
        
    sqlcur.execute('insert into block_validation (id, signature, work) values (?,?,?)', 
        (block_id, signature, work))


def process_send_entry(sqlcur, key, value):

    # blocks.cpp, deserialize_block(stream, type), rai::send_block members

    previous_block = value[:32]
    destination = value[32:64]
    balance = value[64:80]
    signature = value[80:144]
    work = unpack('<Q', value[144:152])[0]
    successor = value[152:184]
    assert len(value[184:]) == 0

    """
    print('Send block %s' % bin2hex(key))
    print('... previous block %s' % bin2hex(previous_block))
    print('... destination %s (%s)' % (destination, encode_account(destination)))
    print('... balance %s (%.6f Mxrb)' % (bin2hex(balance), bin2balance_mxrb(balance)))
    print('... signature %s' % bin2hex(signature))
    print('... work %08x' % work)
    print('... successor %s' % bin2hex(successor))
    """

    block_id = get_block_id(key)

    balance_mxrb = bin2balance_mxrb(balance)
    balance_raw = bin2balance_raw(balance)

    hash = bin2hex(key)
    previous_id = get_block_id(previous_block)
    destination_id = get_account_id(encode_account(destination))
    signature = bin2hex(signature)
    work = '%08x' % work
    successor_id = get_block_id(successor)

    # Note that we store balance_raw (a Python long) as a string
    sqlcur.execute('insert into blocks (id, hash, type, previous, destination, balance, balance_raw, next) values (?,?,?,?,?,?,?,?)',
        (block_id, hash, 'send', previous_id, destination_id, balance_mxrb, str(balance_raw), successor_id))
        
    sqlcur.execute('insert into block_validation (id, signature, work) values (?,?,?)', 
        (block_id, signature, work))


@click.command()
@click.option('-d', '--dbfile', default=DEFAULT_SQLITE_DB, help='SQLite database file', show_default=True)
def create(dbfile):

    """Create SQLite database from the RaiBlocks LMDB database"""

    processor_functions = {
        'change'    : process_change_entry,
        'open'      : process_open_entry,
        'receive'   : process_receive_entry,
        'send'      : process_send_entry,
        #'vote': process_vote_entry,
    }

    print("Reading the Nano database at %s" % RAIBLOCKS_LMDB_DB)

    # Open the RaiBlocks database
    env = lmdb.Environment(
        RAIBLOCKS_LMDB_DB, subdir=False,
        map_size=10*1024*1024*1024, max_dbs=16,
        readonly=True)

    # Initialize sqlite DB

    sqldb = apsw.Connection(dbfile)
    sqlcur = sqldb.cursor()
    #sqlcur.execute('PRAGMA journal_mode=WAL;')
    #sqlcur.execute('PRAGMA synchronous=NORMAL;')    
    sqlcur.execute(SCHEMA)
    sqlcur.execute(DROP_INDICES)

    # Process blocks per type

    for subdbname in ['change', 'open', 'receive', 'send']:

        subdb = env.open_db(subdbname.encode())

        with env.begin(write=False) as tx:
            cur = tx.cursor(subdb)
            cur.first()

            print('Processing "%s" blocks' % subdbname)
            bar = progressbar.ProgressBar(max_value=progressbar.UnknownLength)
            i = 0

            p = processor_functions[subdbname]

            sqlcur.execute('begin')

            for key, value in cur:

                p(sqlcur, key, value)

                i += 1
                bar.update(i)

            sqlcur.execute('commit')

            bar.finish()

    # Store accounts

    print('Storing account info')
    bar = progressbar.ProgressBar(max_value=progressbar.UnknownLength)
    i = 0

    sqlcur.execute('begin')

    for address, id in account_ids.items():

        name = None
        if address in KNOWN_ACCOUNTS:
            name = KNOWN_ACCOUNTS[address]

        sqlcur.execute('insert into accounts (id, address, name) values (?,?,?)',
            (id, address, name))

        i += 1
        bar.update(i)

    sqlcur.execute('commit')

    bar.finish()

@click.command()
@click.option('-d', '--dbfile', default=DEFAULT_SQLITE_DB, help='SQLite database file', show_default=True)
def create_indices(dbfile):
    """Create indices on SQL tables for faster querying"""
    sqldb = apsw.Connection(dbfile)
    sqlcur = sqldb.cursor()
    sqlcur.execute(DROP_INDICES)
    sqlcur.execute(CREATE_INDICES)

@click.command()
@click.option('-d', '--dbfile', default=DEFAULT_SQLITE_DB, help='SQLite database file', show_default=True)
def drop_indices(dbfile):
    """Drop indices"""
    sqldb = apsw.Connection(dbfile)
    sqlcur = sqldb.cursor()
    sqlcur.execute(DROP_INDICES)

@click.command()
@click.option('-d', '--dbfile', default=DEFAULT_SQLITE_DB, help='SQLite database file', show_default=True)
def analyze(dbfile):
    """Let SQLite analyze the tables for improved query performance"""
    sqldb = apsw.Connection(dbfile)
    sqlcur = sqldb.cursor()
    sqlcur.execute('analyze')

@click.command()
@click.option('-d', '--dbfile', default=DEFAULT_SQLITE_DB, help='SQLite database file', show_default=True)
def derive_block_info(dbfile):
    """Store for each block to which account chain (account id) it belongs"""

    sqldb = apsw.Connection(dbfile)
    sqlcur = sqldb.cursor()
    sqlcur.execute('delete from block_info')    

    # Previous point for a block, or None (for open blocks)
    # Key: block id
    # Value: id of previous block
    block_to_previous = {}

    # Get open blocks (as they have an account assigned)

    open_block_to_account = {}
    account_to_open_block = {}

    sqlcur.execute('select id, account from blocks where type=?', ('open',))

    for id, account in sqlcur:
        open_block_to_account[id] = account
        account_to_open_block[account] = id
        block_to_previous[id] = None

    # Gather all other blocks

    blocks_to_process = set()

    sqlcur.execute('select id, previous from blocks where type<>?', ('open',))

    for id, previous in sqlcur:
        if previous is None:
            print('No previous value for block %d!' % id)
            continue

        block_to_previous[id] = previous
        blocks_to_process.add(id)

    # Reconstruct all the account chains, using the previous pointers
    # in the blocks

    # Account chains under construction
    # Key: id of *last* block in the chain
    # Value: list of sequential blocks [open, ... , D, E, ...]; with E.previous = D
    account_chains = {}

    # Bootstrap with the open blocks
    for id in open_block_to_account.keys():
        account_chains[id] = [id]

    print('Reconstructing account chains')
    bar = progressbar.ProgressBar(max_value=progressbar.UnknownLength)
    bar.update(len(blocks_to_process))

    while len(blocks_to_process) > 0:

        # Get next block to process
        tail_block = blocks_to_process.pop()

        #print('processing block %d' % tail_block)

        # Follow the previous pointers until we hit an existing chain
        chain = collections.deque([tail_block])
        blocks_processed = set()

        cur_block = block_to_previous[tail_block]

        while cur_block not in account_chains:

            assert cur_block is not None
            assert cur_block not in blocks_processed

            chain.appendleft(cur_block)
            blocks_processed.add(cur_block)

            cur_block = block_to_previous[cur_block]

        assert cur_block in account_chains

        #print('processed block %d: merging chain of %d with %d' % (tail_block, len(account_chains[cur_block]), len(chain)))

        # Merge the new chain with the existing one
        new_chain = account_chains[cur_block] + list(chain)
        del account_chains[cur_block]
        account_chains[new_chain[-1]] = new_chain

        blocks_to_process -= blocks_processed

        bar.update(len(blocks_to_process))

    bar.finish()

    print('Have %d accounts chains' % len(account_chains))
    assert len(account_chains) == len(open_block_to_account)
    
    # Determine block -> account mapping
    
    block_to_account = {}
    
    for last_block, chain in account_chains.items():
        account = open_block_to_account[chain[0]]        
        for block in chain:
            block_to_account[block] = account
        
    # Perform global topological sort of all blocks
    # (uses block_info results from previous step)
            
    edges = generate_block_dependencies(sqlcur, account_to_open_block, block_to_account)
    
    print('Determining topological order')
    order = topological_sort(edges)
    
    block_to_global_index = {}
    for idx, block in enumerate(order):
        block_to_global_index[block] = idx
        
    # XXX Compute account balance at each block, plus amounts transfered
    # by send/receive/open blocks.
    # Bootstrap with the Genesis account.

    print('Storing per-block info')

    bar = progressbar.ProgressBar(max_value=progressbar.UnknownLength)
    bar.update(len(blocks_to_process))
    i = 0

    sqlcur.execute('begin')        

    for last_block, chain in account_chains.items():

        account = open_block_to_account[chain[0]]
        
        for idx, block in enumerate(chain):            
            sqlcur.execute('insert into block_info (block, account, chain_index, global_index) values (?,?,?,?)', 
                (block, account, idx, block_to_global_index[block]))

        i += 1
        bar.update(i)
        
    sqlcur.execute('commit')        
        
    bar.finish()

"""
@click.command()
@click.option('-d', '--dbfile', default=DEFAULT_SQLITE_DB, help='SQLite database file', show_default=True)
def convert(dbfile):
    "Convert LMDB database to SQLite (all steps)"
    create(dbfile)
    derive_block_info(dbfile)
    create_indices(dbfile)
"""

@click.group()
def cli():
    pass

#cli.add_command(convert)
cli.add_command(create)
cli.add_command(derive_block_info)
cli.add_command(create_indices)
cli.add_command(drop_indices)
cli.add_command(analyze)

if __name__ == '__main__':
    cli()

# XXX add some metadata in the db on when it was generated, command, etc.