from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.document_loaders import PyPDFLoader
from langchain_community.document_loaders import TextLoader
import warnings
import pandas as pd
import os
from redisvl.utils.vectorize import HFTextVectorizer, BaseVectorizer
from redisvl.extensions.cache.embeddings import EmbeddingsCache
from redisvl.index import SearchIndex
from redisvl.redis.utils import array_to_buffer
import redis
from openai import OpenAI
import nltk
import config

try:
    nltk.download('punkt')
except Exception as e:
    print(f"Error downloading NLTK data: {e}")


try :
    nltk.download('punkt_tab')
except Exception as e:
    print(f"Error downloading NLTK data: {e}")

def split_text_into_sentences_nltk(text_file_path):
    print(f"Splitting text from: {text_file_path}, using NLTK")
    with open(text_file_path, 'r', encoding='utf-8') as f:
        text = f.read()
    sentences = nltk.sent_tokenize(text)
    return sentences


warnings.filterwarnings("ignore")
os.environ['TORCHDYNAMO_DISABLE'] = '1'
os.environ['TOKENIZERS_PARALLELISM'] = 'false'
#os.environ['OPENAI_API_KEY'] = "<<open api key>>"

TOPIC = "RAG_SEBI_RHP"
INDEX_NAME = f"IDX_{TOPIC}"
REDIS_URL =  config.REDIS_URL ## "redis://UID:PWD@HOST:PORT" UID-> UserName, PWD-> password, HOST-> hostname/ip of the redis server, PORT-> the respective port.

index = None

schema = {
  "index": {
    "name": INDEX_NAME,
    "prefix": f"{TOPIC}:"
  },
  "fields": [
    {
        "name": "chunk_id",
        "type": "tag",
        "attrs": {
            "sortable": True
        }
    },
    {
        "name": "content",
        "type": "text"
    },
    {
        "name": "text_embedding",
        "type": "vector",
        "attrs": {
            "dims": 768 , # 384, 768
            "distance_metric": "IP",  #cosine, L2, IP
            "algorithm": "HNSW", #  "hnsw", "FLAT", "SVS-VAMANA" #SVS-VAMANA only supports FLOAT16 and FLOAT32 datatypes.
            "datatype":  "FLOAT32", #"float64"
            "m": 16,            # HNSW M (neighbors)
            "ef_construction": 200, # Accuracy vs speed
        }
    }
  ]
}


try:
    index = SearchIndex.from_dict(schema, redis_url=REDIS_URL)
    index.create(overwrite=True, drop=True)
    print("Successfully created index")
except Exception as e:
    print(e)
    exit(0)

hf = HFTextVectorizer(
    model="sentence-transformers/all-MiniLM-L6-v2",
    cache=EmbeddingsCache(
        name=f"embedcache{TOPIC}",
        ttl=600,
        redis_url=REDIS_URL,
    ), dtype= "float32"
)

client = OpenAI()
def get_openai_embedding(text, model="text-embedding-3-large", dimensions=None):
    # Standardize input
    text = text.replace("\n", " ")
    
    # Create the embedding
    # The 'dimensions' parameter is optional for text-embedding-3-large
    # Supported values: 256, 1024, 3072 (default)
    response = client.embeddings.create(
        input=[text],
        model=model,
        dimensions=dimensions
    )    
    return response.data[0].embedding

data_path = "./assets/"
docs = [os.path.join(data_path, file) for file in os.listdir(data_path)]

print("Listing available documents ...", docs)

text_splitter = RecursiveCharacterTextSplitter(
    chunk_size=1000, chunk_overlap=200
)

for doc in docs:
    print(f"Currently processing Doc : ------> {doc} <---------------------")
    #check for the file type and add use the respective type loader.
    #continue if not expected file type

    docid = f"{TOPIC}:{os.path.splitext(os.path.basename(doc))[0]}"
   
    # NLTK & classic REDIS client loop style
    # chunks = split_text_into_sentences_nltk(doc)
    # #embeddings = hf.embed_many([chunk.page_content for chunk in chunks])
    # #embeddings = hf.embed_many([chunk for chunk in chunks])
    
    # data = [
    # {
    #     'chunk_id': f"{docid}_1_{i}",
    #     'content': chunk,
    #     # For HASH -- must convert embeddings to bytes
    #     'text_embedding':    get_openai_embedding (chunk) #array_to_buffer(embeddings[i], dtype='float32')
    # } for i, chunk in enumerate(chunks)
    # ]

    # r = redis.Redis.from_url(REDIS_URL)
    # for i in data :
    #     print(f"{i['chunk_id']}")
    #     r.hset(i['chunk_id'],mapping=i)

    # if text file then
    #loader = TextLoader(doc )
    loader = PyPDFLoader(doc)
    chunks2 = loader.load_and_split(text_splitter)
    embeddings2 = hf.embed_many([chunk.page_content for chunk in chunks2])
    
    data2 = [
    {
        'chunk_id': f"{docid}_2_{i}",
        'content': chunk.page_content,
        # For HASH -- must convert embeddings to bytes
        'text_embedding': array_to_buffer(embeddings2[i], dtype='float64')   # get_openai_embedding(chunk.page_content)
    } for i, chunk in enumerate(chunks2)
    ]
    
    keys = index.load(data2, id_field="chunk_id")
    print("_________________Next___________________________")