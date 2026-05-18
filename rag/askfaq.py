from redisvl.index import SearchIndex
#import boto3 only if using bedrock
import json
import streamlit as st
import os
from redisvl.utils.vectorize import HFTextVectorizer, BaseVectorizer
from redisvl.extensions.cache.embeddings import EmbeddingsCache
from redisvl.query import VectorQuery
from redisvl.extensions.llmcache import SemanticCache
from functools import wraps
import openai
from openai import OpenAI
import os
import time
import requests
import config
import warnings

warnings.filterwarnings("ignore")
os.environ['TORCHDYNAMO_DISABLE'] = '1'
os.environ['TOKENIZERS_PARALLELISM'] = 'false'
#os.environ['OPENAI_API_KEY'] =  "<<The open AI key>>"

TOPIC = "RAG_SEBI_RHP"
INDEX_NAME = f"IDX_{TOPIC}"
REDIS_URL = config.REDIS_URL # "redis://UID:PWD@HOST:PORT" UID-> UserName, PWD-> password, HOST-> hostname/ip of the redis server, PORT-> the respective port.

#async_index = AsyncSearchIndex.from_dict(config.schema, redis_url=config.REDIS_URL)
index = SearchIndex.from_existing(name=INDEX_NAME,redis_url=REDIS_URL)

hf = HFTextVectorizer(
    model="sentence-transformers/all-MiniLM-L6-v2",
    cache=EmbeddingsCache(
        name=f"embedcache{TOPIC}",
        ttl=600,   # short term memory for embedding cache, prevents one roundtrip of API
        redis_url=REDIS_URL,
    ), dtype= "float32"
)

llmcache = SemanticCache(
    name= f"llmcache_{TOPIC}",
    vectorizer=hf,
    redis_url=REDIS_URL,
    #ttl=21600,         # default is no eviction , else specify respective ttl in seconds
    distance_threshold=0.2, # closer the better
    overwrite=False,
)

client = OpenAI()
def get_embedding(text, model="text-embedding-3-large", dimensions=None):
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

# Create an LLM caching decorator
def cache(func):
    @wraps(func)
    def wrapper(index, query_text, *args, **kwargs):
        start_time =  time.time()
        query_vector = llmcache._vectorizer.embed(query_text)
        # Check the cache with the vector
        if result := llmcache.check(vector=query_vector):
            print("---------->It's a cache Hit! #$#")
            end_time = time.time()
            print(f"Time taken to fetch from Cache : {'{:.3f}'.format( end_time - start_time)}")
            return  f"From Cache :: {result[0]['response']} "
               
        print("---------->It's a cache Miss! &&&")
        response = func(index, query_text, query_vector=query_vector)
        res = llmcache.store(query_text, response, query_vector, ttl=6000)
        #print(f'Response from LLM Cache {res}')
        return f"From LLM:: {response}"
    return wrapper

def retrieve_context_sync(index, query_vector):

    """Fetch the relevant context from Redis using vector search"""
    results =  index.query(
        VectorQuery(
            vector=query_vector,
            vector_field_name="text_embedding",
            return_fields=["content"],
            num_results=10, dtype="float64"
        )
    )

    content = "\n".join([result["content"] for result in results])

    return content


SYSTEM_PROMPT = """ROLE: You represent investment advisor as an AI-powered advisor and information expert. 
Your audience includes anyone interested in investing in the Initial Public Offering of the company, 
such as job individual investor, retail investor, small time investor, Investor with large sum of money, industry analysts, or general public. 
Your Core JOB is generate final response to all users based on given 'Context' and 'UserQuery'. 
"""

CHAT_MODEL = "gpt-5.4" #"gpt-3.5-turbo-0125"
#CHAT_MODEL = "gpt-4.1-mini"

@cache
def invokeopenaillm(index, query, **kwargs) :

    print("------------------> Invoking OpenAI LLM<------------------")
    query_vector = hf.embed(query)
    # Fetch context from Redis using vector search
    start_time = time.time()
    context =  retrieve_context_sync(index, query_vector)
    end_time = time.time()

    print(f"The context Length fetched : {len(context)}")
    print(f" Time taken to fetch the context from Redis Vector Index : {'{:.3f}'.format( end_time - start_time) }" )

    
    # Generate contextualized prompt and feed to OpenAI
    start_time = time.time()
    response =  openai.Client().chat.completions.create(
        model=CHAT_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": config.promptfy(query, context)}
        ],
        temperature=0.1,
        seed=42
    )
    end_time = time.time()
    # Response provided by LLM
    #print (response)
    print(f" Time taken to fetch the Response from OpenAI LLM Model : {CHAT_MODEL} : {'{:.3f}'.format( end_time - start_time) }" )
    return response.choices[0].message.content

headers = {
    "access_token" : config.IXHELLOKEY
    ,'Content-Type': 'application/json'
    }

@cache
def invokeixhello(index, query, **kwargs) :
    url = "https://us.api.customer.ixhello.com/v1/Agent/Chat"
    
    print("------------------> Invoking IX Hello LLM<------------------")
    query_vector = hf.embed(query)
    context =  retrieve_context_sync(index, query_vector)
    print (f" the context : {context}")

    payload = {
        "CustomerInput":{query},
        "Context":{context},
        "Guardrail":{GUARD_RAILS}
        }
        
    resp = requests.post(url,json=payload, headers=headers)
    print (resp)
    if(resp.status_code == 200) :
        return resp.content
    
    return "Error invoking LLM"
    
    #for auto testing without ST 
def invokellmdirect(input) :
    return invokeopenaillm(index,input)
    #return invokebedrockllm(index,input)


st.title("Redis RAG for RHP FAQ")
input = st.text_input("Enter your question for FAQ RAG LLM:")
if input :
    
    start_time = time.time()
    resp = invokeopenaillm(index,input)
    #resp = invokeixhello(index,input)
    #resp = invokebedrockllm(index,input)
    end_time = time.time()

    st.write("Response : ", resp, " \n Time taken : ", '{:.3f}'.format( end_time - start_time))
    print(f"Total ellapsed time of execution : {'{:.3f}'.format( end_time - start_time)} ")
    print("______________________________Next Prompt_________________________________")
    print("\n")