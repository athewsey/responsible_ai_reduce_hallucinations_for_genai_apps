#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
import csv
import json
import logging
import os
import re
import shlex
import sqlite3
import subprocess
import sys

import boto3

# pip install custom package to /tmp/ and add to path


# subprocess.Popen(shlex.split('mkdir aim325packages'), stdout=subprocess.PIPE)
p = subprocess.Popen(
    shlex.split(
        "pip install llama-index==0.11.23 ragas==0.2.5 pydantic==2.9.2 datasets==3.1.0 -q -t /tmp/ --no-cache-dir"
    ),
    stdout=subprocess.PIPE,
)  # nosemgrep
out, err = p.communicate()
print(f"out lib install:: {out}")
print(f"err lib install:: {err}")

p = subprocess.Popen(
    shlex.split("pip list | grep ragas"), stdout=subprocess.PIPE
)  # nosemgrep
out, err = p.communicate()
print(f"out lib version :: {out}")
print(f"err lib version:: {err}")

# subprocess.check_output('ls -l /aim325packages/')
sys.path.insert(1, "/tmp/")
"""

# commented for security vulnerability
subprocess.call('pip install ragas pydantic datasets -q -t /tmp/ --no-cache-dir'.split(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
sys.path.insert(1, '/tmp/')
"""

import json
import pprint
import time

import boto3
from botocore.config import Config
from datasets import Dataset
from langchain.chains import RetrievalQA
from langchain.embeddings import BedrockEmbeddings
# from botocore.client import Config
from langchain.llms.bedrock import Bedrock
from langchain.retrievers.bedrock import AmazonKnowledgeBasesRetriever
from langchain_community.chat_models.bedrock import BedrockChat
from ragas import evaluate
from ragas.metrics import answer_correctness, answer_relevancy

credentials_profile_name = "default"
session = boto3.session.Session()
region_name = session.region_name
account_number = boto3.client("sts").get_caller_identity().get("Account")


# increase the standard time out limits in boto3, because Bedrock may take a while to respond to large requests.
my_config = Config(
    connect_timeout=60 * 10,
    read_timeout=60 * 10,
)
bedrock_client = boto3.client(service_name="bedrock-runtime", config=my_config)
bedrock_service = boto3.client(service_name="bedrock", config=my_config)
bedrock_agent_client = boto3.client("bedrock-agent-runtime", config=my_config)

# bedrock_config = Config(connect_timeout=1000, read_timeout=1000, retries={'max_attempts': 5})
# bedrock_client = session.client(service_name="bedrock",config=retry_config,**client_kwargs )

# bedrock_client = boto3.client('bedrock-runtime',  config=bedrock_config)
# bedrock_agent_client = boto3.client("bedrock-agent-runtime", config=bedrock_config)

sonnet = "anthropic.claude-3-sonnet-20240229-v1:0"
haiku = "anthropic.claude-3-haiku-20240307-v1:0"

# llm_for_text_generation = BedrockChat(model_id=sonnet, client=bedrock_client)
llm_for_evaluation = BedrockChat(model_id=sonnet, client=bedrock_client)
bedrock_embeddings = BedrockEmbeddings(
    model_id="amazon.titan-embed-text-v2:0", client=bedrock_client
)


# setting logger
logging.basicConfig(
    format="[%(asctime)s] p%(process)s {%(filename)s:%(lineno)d} %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


s3 = boto3.client("s3")
sns_client = boto3.client("sns")
bucket = os.environ.get("BUCKET_NAME")  # Name of bucket with data file and OpenAPI file
kb_prefix = os.environ.get("KB_PREFIX")
topic_name = os.environ.get("SNS_TOPIC_NAME")

csv_name = "reinvent2024-hallucinations-questions.csv"
csvfile = s3.get_object(Bucket=bucket, Key=csv_name)

data = csvfile["Body"].read().decode("utf-8").splitlines()


def get_ground_truth_for_question(question):
    ground_truth_ans = None

    lines = csv.reader(data)
    headers = next(lines)
    print("headers: %s" % (headers))

    for line in lines:
        if line[1].lower() == question.lower():
            ground_truth_ans = line[2]

    print(f"ground_truth_ans :: {ground_truth_ans}")
    return ground_truth_ans


def ragas_evaluation(question, kb_response):

    ground_truth_ans = get_ground_truth_for_question(question)
    data_samples = {
        "question": [question],
        "answer": [kb_response],
        "ground_truth": [ground_truth_ans],
    }

    dataset = Dataset.from_dict(data_samples)
    score = evaluate(
        dataset,
        metrics=[answer_correctness, answer_relevancy],
        llm=llm_for_evaluation,
        embeddings=bedrock_embeddings,
    )

    print(f"score[answer_correctness] :: {score['answer_correctness']}")
    print(type(score["answer_correctness"]))

    print(f"score[answer_relevancy] :: {score['answer_relevancy']}")
    print(type(score["answer_relevancy"]))

    if isinstance(score["answer_correctness"], list) and isinstance(
        score["answer_relevancy"], list
    ):
        avg_score = (score["answer_correctness"][0] + score["answer_relevancy"][0]) / 2
    else:
        avg_score = (score["answer_correctness"] + score["answer_relevancy"]) / 2

    print(f"avg_score :: {avg_score}")

    return avg_score, ground_truth_ans


def send_sns_notification(question, kb_response, ground_truth, hallucinationScore):

    # add sns call to customer service queue - separate notebook to see queue
    complete_arn = f"arn:aws:sns:{region_name}:{account_number}:{topic_name}"
    complete_message_to_topic = f"Customer needs help with the following question :: {question}. The AI workflow response quality does not meet the quality threshold {kb_response}. The ground truth answer is {ground_truth}. Please join the customer chat to assist them further. Thank you."
    response = sns_client.publish(
        TopicArn=complete_arn, Message=complete_message_to_topic
    )
    print(
        f"Message = {complete_message_to_topic} published to topic = {topic_name}. SNS response = {response}"
    )

    return response


def measure_hallucination(question, kb_response):
    hallucinationScore, ground_truth = ragas_evaluation(question, kb_response)
    threshold = 0.85
    response = None
    if hallucinationScore < threshold:
        response = f"For question = {question} .. Getting a customer service agent to help you, please wait and stay connected .... "
        send_sns_notification(question, kb_response, ground_truth, hallucinationScore)
    else:
        response = kb_response
    print(f"Response from measure_hallucination :: {response}")

    response_json = {
        "response": {
            "finalAPIResponse": response,
            "kbResponse": kb_response,
            "hallucinationScore": hallucinationScore,
        }
    }
    return response_json


def process_sns_message(record):
    try:
        message = record["Sns"]["Message"]
        print(f"Received SNS message ::  {message}")
        return message

    except Exception as e:
        print("An error occurred during SNS message processing .. ")
        raise e


def lambda_handler(event, context):
    print("Entered lambda_handler >>>>>>> ")
    print(f"event >>>>> {event}")
    sns_message = None

    # SNS message processing
    if "Records" in event:
        for record in event["Records"]:
            sns_message = process_sns_message(record)
        print("Finished processing SNS message")

    # get the action group used during the invocation of the lambda function
    actionGroup = event.get("actionGroup", "")

    # name of the function that should be invoked
    function = event.get("function", "")

    # parameters to invoke function with
    parameters = event.get("parameters", [])

    # action = event['actionGroup']
    # api_path = event['apiPath']
    question = None
    kb_response = None
    # Get the query value from the parameters
    if parameters is not None and len(parameters) > 0:
        question = parameters[0]["value"]
        kb_response = parameters[1]["value"]
        print(f"question: {question} and kb_response :: {kb_response}")

    if function == "detect_measure_hallucination":
        print("About to call measure-hallucination() >>>>>>> ")
        response_json = measure_hallucination(question, kb_response)
    elif "Records" in event:
        response_json = {f"SNS message received is {sns_message}"}
    else:
        response_json = {
            "{}::{} is not a valid api, try another one.".format(action, api_path)
        }

    response_body = {"TEXT": {"body": str(response_json)}}

    action_response = {
        "actionGroup": actionGroup,
        "function": function,
        "functionResponse": {"responseBody": response_body},
    }

    function_response = {"response": action_response}
    print("Response: {}".format(function_response))
    return function_response
