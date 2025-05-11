import os
from datetime import datetime
from pymongo import MongoClient
import numpy as np
from tqdm import tqdm

# Global placeholder, will be injected by server.py
mongo_client = None

def get_db_collections():
    db = mongo_client.wavelength
    return db.games, db.rounds

### 1. Load GloVe embeddings for cosine similarity calculations ###
def load_glove(fpath):
    embeddings = {}
    with open(fpath, 'r', encoding='utf-8') as f:
        for line in tqdm(f, desc="Loading GloVe"):
            parts = line.strip().split()
            word = parts[0]
            vec  = np.array(parts[1:], dtype=np.float32)
            embeddings[word] = vec
    return embeddings

def cos_sim(a, b):
    """
    Compute cosine similarity between two vectors a and b.
    Returns a Python float between -1.0 and 1.0.
    """
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))

GLOVE_PATH = os.environ.get('GLOVE_PATH', 'glove.6B.100d.txt')
glove_embeddings = load_glove(GLOVE_PATH)

### 2. Helper functions ###
def get_optimal_word(gameID, roundNumber):
    """
    For roundNumber > 1, fetch previous round's two guesses,
    compute their centroid embedding, and find the vocab word
    with highest cosine similarity to that centroid. Returns None
    if insufficient data or embeddings missing.
    """
    games_coll, rounds_coll = get_db_collections()
    if roundNumber <= 1:
        return None
    prev = rounds_coll.find_one({'gameID': gameID, 'roundNumber': roundNumber - 1})
    if not prev or len(prev.get('guesses', [])) < 2:
        return None
    vecs = []
    for g in prev['guesses']:
        v = glove_embeddings.get(g['word'])
        if v is None:
            return None
        vecs.append(v)
    centroid = np.mean(vecs, axis=0)
    best_word = None
    best_sim  = -1.0
    for w, vec in glove_embeddings.items():
        sim = cos_sim(vec, centroid)
        if sim > best_sim:
            best_sim  = sim
            best_word = w
    return best_word

def compute_round_results(gameID, roundNumber):
    """
    Fetch the round doc, compute each guess's similarity to optimalWord,
    and the similarity between the two guesses (playerSim).
    Returns (entries_list, playerSim).
    """
    games_coll, rounds_coll = get_db_collections()
    rd = rounds_coll.find_one({'gameID': gameID, 'roundNumber': roundNumber})
    optimal_word = rd.get('optimalWord')
    optimal_vec  = glove_embeddings.get(optimal_word)

    entries = []
    guess_vecs = []
    for guess in rd.get('guesses', []):
        w   = guess['word']
        vec = glove_embeddings.get(w)
        sim = cos_sim(vec, optimal_vec) if (vec is not None and optimal_vec is not None) else None
        entries.append({
            'userID':       guess['userID'],
            'word':         w,
            'optimalSim':   sim
        })
        guess_vecs.append(vec)

    # compute similarity between the two guess vectors
    if len(guess_vecs) == 2 and None not in guess_vecs:
        playerSim = cos_sim(guess_vecs[0], guess_vecs[1])
    else:
        playerSim = None
    return entries, playerSim


def serialize_doc(doc):
    d = doc.copy()
    d.pop('_id', None)
    return d

def get_full_game_state(gameID):
    """
    Helper to fetch and serialize a game with its full round history.
    """
    games_coll, rounds_coll = get_db_collections()
    game = games_coll.find_one({'gameID': gameID})
    if not game:
        return None
    rounds = list(rounds_coll.find({'gameID': gameID}).sort('roundNumber', 1))
    payload = serialize_doc(game)
    payload['rounds'] = [serialize_doc(r) for r in rounds]
    return payload

### 3. Service Functions ###

# 3.1.1 Create a new game
def create_game(gameID, p1):
    '''
    Creates a new game doc
    '''
    games_coll, _ = get_db_collections()
    if not all([gameID, p1]):
        return {'error': 'Missing gameID or player1ID', 'status_code': 400}
    now = datetime.utcnow()
    game = {
        'gameID': gameID,
        'player1ID': p1,
        'player2ID': None,
        'currentRoundNumber': None,
        'status': 'pending',
        'createdAt': now,
        'updatedAt': now
    }
    games_coll.insert_one(game)
    return serialize_doc(game)

# 3.1.2 Join a game
def join_game(gameID, p2):
    '''
    Updates a game doc to include a new player
    '''
    games_coll, _ = get_db_collections()
    if not all([gameID, p2]):
        return {'error': 'Missing gameID or player2ID', 'status_code': 400}
    now = datetime.utcnow()
    res = games_coll.update_one(
        {'gameID': gameID, 'status': 'pending'},
        {'$set': {
            'player2ID': p2,
            'status': 'in_progress',
            'currentRoundNumber': 1,
            'updatedAt': now
        }}
    )
    if res.matched_count == 0:
        return {'error': 'Game not found or not pending', 'status_code': 404}
    game = games_coll.find_one({'gameID': gameID})
    return serialize_doc(game)

# 3.2 Add a move (and possibly end or advance the game)
def add_move(gameID, rn, userID, word):
    games_coll, rounds_coll = get_db_collections()
    if not userID or not word:
        return {'error':'Missing userID or word', 'status_code':400}

    # Validate game & player IDs
    game = games_coll.find_one({'gameID': gameID})
    if not game or game['status'] != 'in_progress':
        return {'error':'Game not in progress', 'status_code':400}
    if userID not in {game.get('player1ID'), game.get('player2ID')}:
        return {'error':'Invalid player', 'status_code':403}

    now     = datetime.utcnow()
    optimal = get_optimal_word(gameID, rn)

    # Upsert round with the guess
    rounds_coll.update_one(
        {'gameID': gameID, 'roundNumber': rn},
        {
          '$setOnInsert': {
            'gameID':      gameID,
            'roundNumber': rn,
            'optimalWord': optimal,
            'stage':       'collecting',
            'startedAt':   now
          },
          '$push': {
            'guesses': {
              'userID':    userID,
              'word':      word,
              'timestamp': now
            }
          }
        },
        upsert=True
    )

    rd = rounds_coll.find_one({'gameID': gameID, 'roundNumber': rn})
    if len(rd.get('guesses', [])) < 2:
        return {'success':True,'roundStage':'collecting'}

    # Score the round
    entries, playerSim = compute_round_results(gameID, rn)
    rounds_coll.update_one(
        {'gameID': gameID, 'roundNumber': rn},
        {'$set': {
            'results.entries':   entries,
            'results.playerSim': playerSim,
            'stage':             'scored',
            'endedAt':           now
        }}
    )

    # Win if same word
    same = (entries[0]['word'] == entries[1]['word'])
    if same:
        games_coll.update_one(
            {'gameID': gameID},
            {'$set': {'status': 'won', 'updatedAt': now}}
        )
    else:
        games_coll.update_one(
            {'gameID': gameID},
            {'$inc': {'currentRoundNumber': 1},
             '$set': {'updatedAt': now}}
        )

    # Return full game state
    payload = get_full_game_state(gameID)
    return {'success':True,'roundStage':'scored','game':payload}

# 3.3 Get game history
def get_game(gameID):
    '''
    Gets the full details for a game
    '''
    payload = get_full_game_state(gameID)
    return state if state else None

# 3.4 Quit game (mark as lost)
def quit_game(gameID):
    '''
    Update a game state to reflect a user ended it ("gave up")
    '''
    games_coll, _ = get_db_collections()
    res = games_coll.update_one(
        {'gameID': gameID},
        {'$set': {'status': 'lost', 'updatedAt': datetime.utcnow()}}
    )
    if res.matched_count == 0:
        return {'error':'Game not found'}

    payload = get_full_game_state(gameID)
    return payload

# 3.5 Get user's games
def get_user_games(user_id):
    '''
    Gets IDs of all games that a user is involved in (joined or created)
    '''
    games_coll, _ = get_db_collections()
    cursor = games_coll.find(
        {'$or': [{'player1ID': user_id}, {'player2ID': user_id}]},
        {'gameID': 1, '_id': 0}
    )
    return [g['gameID'] for g in cursor]
