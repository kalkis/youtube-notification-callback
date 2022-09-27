import xmltodict
import boto3
import os
import json


def lambda_handler(event, context):
    xml = xmltodict.parse(event['body'])
    topic_name = os.getenv('TOPIC_NAME', 'youtube-pubsubhubbub')
    info = extract_info(xml)
    write_to_topic(topic_name, info)
    return {
        "status": 200,
        "topic_name": topic_name,
        "info": json.dumps(info)
    }


def extract_info(xml):
    info = xml['feed']['entry']
    return {
        'channel_id': info['yt:channelId'],
        'channel_name': info['author']['name'],
        'channel_link': info['author']['uri'],
        'video_name': info['title'],
        'video_id': info['yt:videoId'],
        'youtube_link': info['link']['@href'],
        'published': info['published'],
        'updated': info['updated']
    }


def write_to_topic(topic_name, info):
    sns = boto3.client('sns')
    # creates topic if it does not exist, or returns existing topic
    topic = sns.create_topic(Name=topic_name)
    sns.publish(TopicArn=topic['TopicArn'], Message=json.dumps(info))

