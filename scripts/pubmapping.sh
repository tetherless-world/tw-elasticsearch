#!/bin/sh

curl -XPUT http://localhost:9200/tw/publication/_mapping -d @mappings/publication.json
