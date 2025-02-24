import os
import requests
from bs4 import BeautifulSoup
import google.generativeai as genai
import tweepy
import time
from flask import Flask, render_template, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
import logging
from datetime import datetime
import pytz
from flask_socketio import SocketIO
import queue
from threading import Lock
from dotenv import load_dotenv
import json

load_dotenv()

app = Flask(__name__)
socketio = SocketIO(app)
tweet_queue = queue.Queue(maxsize=100)  # Store last 100 tweets
log_queue = queue.Queue(maxsize=100)    # Store last 100 log messages
thread_lock = Lock()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Custom handler to capture logs for the UI
class QueueHandler(logging.Handler):
    def emit(self, record):
        log_entry = {
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'level': record.levelname,
            'message': record.getMessage()
        }
        log_queue.put(log_entry)
        socketio.emit('log_update', log_entry)
        
        # Save log to file
        with open('logs.json', 'a') as log_file:
            log_file.write(json.dumps(log_entry) + '\n')

logger.addHandler(QueueHandler())

class NewsToTweetAutomation:
    def __init__(self, google_api_key, twitter_credentials):
        """
        Initialize with necessary API keys and credentials
        """
        self.google_api_key = google_api_key
        self.twitter_credentials = twitter_credentials
        
        # Initialize Google Gemini
        genai.configure(api_key=self.google_api_key)
        self.model = genai.GenerativeModel('gemini-2.0-flash')
        
        # Initialize Twitter API v2
        self.twitter_client = tweepy.Client(
            consumer_key=twitter_credentials['consumer_key'],
            consumer_secret=twitter_credentials['consumer_secret'],
            access_token=twitter_credentials['access_token'],
            access_token_secret=twitter_credentials['access_token_secret']
        )

        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }

    def get_latest_articles(self):
        """
        Fetch and parse the TechCrunch AI page
        """
        try:
            url = 'https://techcrunch.com/category/artificial-intelligence/'
            logger.info(f"Fetching articles from {url}")
            response = requests.get(url, headers=self.headers)
            response.raise_for_status()
            soup = BeautifulSoup(response.content, 'html.parser')
            
            articles = []
            article_elements = soup.find_all('div', class_='loop-card__content')
            
            for article in article_elements[:2]:  # Reduced to 2 articles for serverless
                link_element = article.find('a', class_='loop-card__title-link')
                if link_element:
                    url = link_element.get('href')
                    title = link_element.text.strip()
                    if url and title:
                        articles.append({
                            'url': url,
                            'title': title
                        })
            
            logger.info(f"Found {len(articles)} articles")
            return articles
            
        except Exception as e:
            logger.error(f"Error fetching articles: {e}")
            return []

    def scrape_article(self, url):
        """
        Scrape article content from URL
        """
        try:
            logger.info(f"Scraping article from {url}")
            response = requests.get(url, headers=self.headers)
            response.raise_for_status()
            soup = BeautifulSoup(response.content, 'html.parser')
            
            title = soup.find('h1').text.strip()
            content_elements = soup.find_all(['p', 'h2'])
            article_text = '\n'.join([elem.text.strip() for elem in content_elements if elem.text.strip()])
            
            logger.info(f"Scraped article titled: {title}")
            return {
                'title': title,
                'content': article_text,
                'url': url
            }
            
        except Exception as e:
            logger.error(f"Error scraping article: {e}")
            return None

    def generate_tweets(self, article):
        """
        Generate tweets using Google Gemini
        """
        try:
            prompt = f"""
            Create 2 engaging tweets about this tech news article. Requirements:
            1. Each tweet must be exactly 280 characters
            2. Tweet should tell properly what is actually happening based on the article.
            3. Include relevant hashtags
            4. Do not add tweet with reference or URL
            5. Return ONLY the tweets, no introductory text or labels
            6. Separate the tweets with a triple dash: ---
            
            Article Title: {article['title']}
            Article Content: {article['content']}
            URL: {article['url']}
            """
            
            response = self.model.generate_content(prompt)
            tweets = [tweet.strip() for tweet in response.text.split('---')]
            tweets = [t for t in tweets if t and not t.startswith(('Tweet', 'Here are', '**'))]
            
            # Store generated tweets for UI display and save to file
            with open('tweets.json', 'a') as tweet_file:
                for tweet in tweets[:2]:
                    tweet_entry = {
                        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                        'content': tweet,
                        'article_title': article['title']
                    }
                    tweet_queue.put(tweet_entry)
                    socketio.emit('tweet_update', tweet_entry)
                    tweet_file.write(json.dumps(tweet_entry) + '\n')
            
            logger.info(f"Generated {len(tweets)} tweets for article: {article['title']}")
            return tweets[:2]
            
        except Exception as e:
            logger.error(f"Error generating tweets: {e}")
            return []

    def post_tweet(self, tweet_text):
        """
        Post tweet to Twitter with retry mechanism
        """
        max_retries = 5
        retry_delay = 1  # Initial delay in seconds

        for attempt in range(max_retries):
            try:
                tweet_text = tweet_text.strip()
                if not tweet_text or tweet_text.startswith(('Tweet', 'Here are', '**')):
                    return None
                
                response = self.twitter_client.create_tweet(text=tweet_text)
                logger.info(f"Tweet posted successfully: {tweet_text[:50]}...")
                return response
            except tweepy.TooManyRequests as e:
                logger.error(f"Error posting tweet: {e}")
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                    retry_delay *= 2  # Exponential backoff
                else:
                    return None
            except Exception as e:
                logger.error(f"Error posting tweet: {e}")
                return None

    def run_automation(self):
        """
        Main function to run the automation
        """
        logger.info("Starting automation run")
        articles = self.get_latest_articles()
        
        tweets_posted = 0
        for article in articles:
            if tweets_posted >= 4:  # Limit to 4 tweets per run
                break
                
            logger.info(f"Processing article: {article['title']}")
            
            article_data = self.scrape_article(article['url'])
            if not article_data:
                continue
            
            tweets = self.generate_tweets(article_data)
            
            time.sleep(2)
            
            # Post 2 tweets for the same article
            for tweet in tweets[:2]:
                if len(tweet) <= 280:
                    self.post_tweet(tweet)
                    tweets_posted += 1
                    time.sleep(5)
            
            if tweets_posted >= 4:
                break

# Initialize automation instance
google_api_key = os.getenv('GEMINI_API_KEY')
twitter_credentials = {
        "consumer_key": os.getenv('CONSUMER_KEY'),
        "consumer_secret": os.getenv("CONSUMER_SECRET"),
        "access_token": os.getenv("ACCESS_TOKEN"),
        "access_token_secret": os.getenv("ACCESS_TOKEN_SECRET")
}

automation = NewsToTweetAutomation(google_api_key, twitter_credentials)

# Initialize scheduler
scheduler = BackgroundScheduler()
ist_timezone = pytz.timezone('Asia/Kolkata')

# Schedule jobs
scheduler.add_job(automation.run_automation, 'cron', hour=11, minute=0, timezone=ist_timezone)
scheduler.add_job(automation.run_automation, 'cron', hour=13, minute=0, timezone=ist_timezone)
scheduler.add_job(automation.run_automation, 'cron', hour=15, minute=0, timezone=ist_timezone)
scheduler.add_job(automation.run_automation, 'cron', hour=18, minute=0, timezone=ist_timezone)
scheduler.start()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/get_logs')
def get_logs():
    logs = list(log_queue.queue)
    return jsonify(logs)

@app.route('/get_tweets')
def get_tweets():
    tweets = list(tweet_queue.queue)
    return jsonify(tweets)

if __name__ == '__main__':
    if not scheduler.running:
        scheduler.start()
    socketio.run(app, debug=False)
