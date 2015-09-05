__author__ = 'szednik'

from SPARQLWrapper import SPARQLWrapper, JSON, XML
import json
from rdflib import Namespace, RDF, URIRef
import multiprocessing
import itertools
import argparse
import requests
import warnings
import pprint


def load_file(filepath):
    with open(filepath) as _file:
        return _file.read().replace('\n', " ")


get_publications_query = load_file("queries/listPublications.rq")
describe_publication_query = load_file("queries/describePublication.rq")

FOAF = Namespace("http://xmlns.com/foaf/0.1/")
TW = Namespace("http://tw.rpi.edu/schema/")
SKOS = Namespace("http://www.w3.org/2008/05/skos#")
DCT = Namespace("http://purl.org/dc/terms/")


def get_metadata(id):
    return {"index": {"_index": "tw", "_type": "publication"}}


def select(endpoint, query):
    sparql = SPARQLWrapper(endpoint)
    sparql.setQuery(query)
    sparql.setReturnFormat(JSON)
    results = sparql.query().convert()
    return results["results"]["bindings"]


def describe(endpoint, query):
    sparql = SPARQLWrapper(endpoint)
    sparql.setQuery(query)
    sparql.setReturnFormat(XML)
    try:
        result = sparql.query()
        resultc = result.convert()
        return resultc
    except RuntimeWarning:
        pass


def get_publications(endpoint):
    r = select(endpoint, get_publications_query)
    return [rs["publication"]["value"] for rs in r]


def process_publication(publication, endpoint):
    pub = create_publication_doc(publication=publication, endpoint=endpoint)
    if "uri" in pub and pub["uri"] is not None:
        return [json.dumps(get_metadata(pub["uri"])), json.dumps(pub)]
    else:
        return []


def describe_publication(endpoint, publication):
    q = describe_publication_query.replace("?publication", "<" + publication + ">")
    return describe(endpoint, q)


def create_publication_doc(publication, endpoint):
    graph = describe_publication(endpoint=endpoint, publication=publication)
#    print( graph.serialize(format='n3') )

    pub = graph.resource(publication)

    try:
        title = list(pub.objects(DCT.title))
        title = title[0].toPython()
    except AttributeError:
        print("missing title:", publication)
        return {}

    doc = {"uri": publication, "title": title}

    abstract = pub.value(TW.hasAbstract)
    abstract = abstract if abstract else None
    if abstract:
        doc.update({"abstract": abstract})

    page = list(pub.objects(TW.page))
    page = str(page[0].identifier) if page else None
    if page:
        doc.update({"page": page})

    event = list(pub.objects(TW.inEvent))
    event = event[0] if event else None
    if event and event.value(FOAF.name):
        doc.update({"presentedAt": {"uri": str(event.identifier), "name": event.value(FOAF.name)}})
    elif event:
        print("event missing label:", str(event.identifier))

    venue = list(pub.objects(TW.inPublication))
    venue = venue[0] if venue else None
    if venue and venue.value(DCT.title):
        doc.update({"publishedIn": {"uri": str(venue.identifier), "name": venue.value(DCT.title)}})
    elif venue:
        print("venue missing label:", str(venue.identifier))

    topics = []
    for topic in pub.objects(TW.hasTopic):
        sa = {"uri": str(topic.identifier)}
        if topic.value(SKOS.prefLabel):
            sa.update({"name": topic.value(SKOS.prefLabel)})
        topics.append(sa)

    if topics:
        doc.update({"topic": topics})

    projects = []
    for project in pub.objects(TW.hasProjectReference):
        p = {"uri": str(project.identifier)}
        if project.value(FOAF.name):
            p.update({"name": project.value(FOAF.name)})
        projects.append(p)

    if projects:
        doc.update({"project": projects})

    themes = []
    for theme in pub.objects(TW.hasThemeReference):
        t = {"uri": str(theme.identifier)}
        if theme.value(FOAF.name):
            t.update({"name": theme.value(FOAF.name)})
        themes.append(t)

    if themes:
        doc.update({"researchArea": projects})

    people = list(graph.subjects(RDF.type, FOAF.Person))
    authors = []
    authorships = list(pub.objects(TW.hasAgentWithRole))
    for authorship in authorships:
        for a in people:
            person = graph.resource(a)
            roles = person.objects(TW.hasRole)
            for r in roles:
                if r == authorship:
                    name = person.value(FOAF.name)
                    ri = r.identifier
                    for s, p, o in graph.triples((ri, URIRef("http://tw.rpi.edu/schema/index"), None)):
                        rank = o
                    obj = {"uri": str(person.identifier), "name": name, "rank": rank }
                    authors.append(obj)

    try:
        authors = sorted(authors, key=lambda a: a["rank"]) if len(authors) > 1 else authors
    except KeyError:
        print("missing rank for one or more authors of:", publication)

    doc.update({"authors": authors})

    return doc


def has_type(resource, type):
    for rtype in resource.objects(RDF.type):
        if str(rtype.identifier) == str(type):
            return True
    return False


def publish(bulk, endpoint, rebuild, mapping):
    # if configured to rebuild_index
    # Delete and then re-create to publication index (via PUT request)

    index_url = endpoint + "/dco"

    if rebuild:
        requests.delete(index_url)
        r = requests.put(index_url)
        if r.status_code != requests.codes.ok:
            print(r.url, r.status_code)
            r.raise_for_status()

    # push current publication document mapping

    mapping_url = endpoint + "/dco/publication/_mapping"
    with open(mapping) as mapping_file:
        r = requests.put(mapping_url, data=mapping_file)
        if r.status_code != requests.codes.ok:

            # new mapping may be incompatible with previous
            # delete current mapping and re-push

            requests.delete(mapping_url)
            r = requests.put(mapping_url, data=mapping_file)
            if r.status_code != requests.codes.ok:
                print(r.url, r.status_code)
                r.raise_for_status()

    # bulk import new publication documents
    bulk_import_url = endpoint + "/_bulk"
    r = requests.post(bulk_import_url, data=bulk)
    if r.status_code != requests.codes.ok:
        print(r.url, r.status_code)
        r.raise_for_status()


def generate(threads, sparql):
    pool = multiprocessing.Pool(threads)
    params = [(publication, sparql) for publication in get_publications(endpoint=sparql)]
    return list(itertools.chain.from_iterable(pool.starmap(process_publication, params)))


if __name__ == "__main__":

    parser = argparse.ArgumentParser()
    parser.add_argument('--threads', default=8, help='number of threads to use (default = 8)')
    parser.add_argument('--es', default="http://data.deepcarbon.net/es", help="elasticsearch service URL")
    parser.add_argument('--publish', default=False, action="store_true", help="publish to elasticsearch?")
    parser.add_argument('--rebuild', default=False, action="store_true", help="rebuild elasticsearch index?")
    parser.add_argument('--mapping', default="mappings/publication.json", help="publication elasticsearch mapping document")
    parser.add_argument('--sparql', default='http://tw.rpi.edu/endpoint/books', help='sparql endpoint')
    parser.add_argument('out', metavar='OUT', help='elasticsearch bulk ingest file')

    args = parser.parse_args()

    # generate bulk import document for publications
    records = generate(threads=int(args.threads), sparql=args.sparql)

    # save generated bulk import file so it can be backed up or reviewed if there are publish errors
    with open(args.out, "w") as bulk_file:
        bulk_file.write('\n'.join(records))

    # publish the results to elasticsearch if "--publish" was specified on the command line
    if args.publish:
        bulk_str = '\n'.join(records)
        publish(bulk=bulk_str, endpoint=args.es, rebuild=args.rebuild, mapping=args.mapping)
