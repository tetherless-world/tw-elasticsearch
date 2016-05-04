#!/bin/sh

curl -XPOST http://localhost:9200/_bulk --data-binary @bulk-20160426.json

