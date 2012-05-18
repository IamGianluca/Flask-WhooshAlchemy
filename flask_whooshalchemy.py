'''

    whooshalchemy flask extension
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    Adds whoosh indexing capabilities to SQLAlchemy models for Flask
    applications.

    :copyright: (c) 2012 by Karl Gyllstrom
    :license: BSD (see LICENSE.txt)

'''

from __future__ import with_statement
from __future__ import absolute_import


from flask_sqlalchemy import models_committed

import sqlalchemy

from whoosh.qparser import OrGroup
from whoosh.qparser import AndGroup
from whoosh.qparser import MultifieldParser
from whoosh.analysis import StemmingAnalyzer
import whoosh.index
from whoosh.fields import Schema
#from whoosh.fields import ID, TEXT, KEYWORD, STORED

import heapq
import os


def _EmptyQuery(model):
    ''' Used to return empty set when whoosh-search results return nothing. '''

    # XXX is this efficient?
    return model.__class__.query.filter('null')


class _QueryProxy(sqlalchemy.orm.Query):
    # We're replacing the model's ``query`` field with this proxy. The main
    # thing this proxy does is override the __iter__ method so that results are
    # returned in the order of the whoosh score to reflect text-based ranking.

    def __init__(self, query_obj, primary_key_name, whoosh_searcher, model):

        # Make this a pure copy of the original Query object.
        self.__dict__ = query_obj.__dict__.copy()

        self._primary_key_name = primary_key_name
        self._whoosh_searcher = whoosh_searcher
        self._model = model

        # Stores whoosh results from query. If ``None``, indicates that no
        # whoosh query was performed.

        self._whoosh_rank = None

    def __iter__(self):
        ''' Reorder ORM-db results according to Whoosh relevance score. '''

        super_iter = super(_QueryProxy, self).__iter__()

        if self._whoosh_rank is None:
            # Whoosh search hasn't been run so behave as normal.

            return super_iter

        # Iterate through the values and re-order by whoosh relevance.
        ordered_by_whoosh_rank = []

        for row in super_iter:
            # Push items onto heap, where sort value is the rank provided by
            # Whoosh

            heapq.heappush(ordered_by_whoosh_rank,
                (self._whoosh_rank[unicode(getattr(row,
                    self._primary_key_name))], row))

        def _inner():
            while ordered_by_whoosh_rank:
                yield heapq.heappop(ordered_by_whoosh_rank)[1]

        return _inner()

    def whoosh_search(self, query, limit=None, fields=None, or_=False):
        '''
        
        Execute text query on database. Results have a text-based
        match to the query, ranked by the scores from the underlying Whoosh index.

        By default, the search is executed on all of the indexed fields as an
        OR conjunction. For example, if a model has 'title' and 'content'
        indicated as ``__searchable__``, a query will be checked against both
        fields, returning any instance whose title or content are a content
        match for the query. To specify particular fields to be checked,
        populate the ``fields`` parameter with the desired fields.

        By default, results will only be returned if they contain all of the
        query terms (AND). To switch to an OR grouping, set the ``or_``
        parameter to ``True``.

        '''

        if not isinstance(query, unicode):
            query = unicode(query)

        results = self._whoosh_searcher(query, limit, fields, or_)

        if len(results) == 0:
            return _EmptyQuery(self._model)

        result_set = set()
        result_ranks = {}

        for rank, result in enumerate(results):
            pk = result[self._primary_key_name]
            result_set.add(pk)
            result_ranks[pk] = rank

        f = self.filter(getattr(self._model.__class__,
            self._primary_key_name).in_(result_set))

        f._whoosh_rank = result_ranks

        return f


class _Searcher(object):
    ''' Assigned to a Model class as ``pure_search``, which enables
    text-querying to whoosh hit list. Also used by ``query.whoosh_search``'''

    def __init__(self, primary, indx):
        self.primary_key_name = primary
        self._index = indx
        self.searcher = indx.searcher()
        self._all_fields = list(set(indx.schema._fields.keys()) -
                set([self.primary_key_name]))

    def __call__(self, query, limit=None, fields=None, or_=False):
        if fields is None:
            fields = self._all_fields

        group = OrGroup if or_ else AndGroup
        parser = MultifieldParser(fields, self._index.schema, group=group)
        return self._index.searcher().search(parser.parse(query),
                limit=limit)


def _whoosh_index(app, model):
    # gets the whoosh index for this model, creating one if it does not exist.
    # in creating one, a schema is created based on the fields of the model.
    # Currently we only support primary key -> whoosh.ID, and sqlalchemy.TEXT
    # -> whoosh.TEXT, but can add more later. A dict of model -> whoosh index
    # is added to the ``app`` variable.

    if not hasattr(app, 'whoosh_indexes'):
        app.whoosh_indexes = {}

    indx = app.whoosh_indexes.get(model.__class__.__name__)

    if indx is None:
        if not app.config.get('WHOOSH_BASE'):
            # XXX todo: is there a better approach to handle the absenSe of a
            # config value for whoosh base? Should we throw an exception? If
            # so, this exception will be thrown in the after_commit function,
            # which is probably not ideal.

            app.config['WHOOSH_BASE'] = 'whoosh_index'

        # we index per model.
        wi = os.path.join(app.config.get('WHOOSH_BASE'),
                model.__class__.__name__)

        schema, primary = _get_whoosh_schema_and_primary(model)

        if whoosh.index.exists_in(wi):
            indx = whoosh.index.open_dir(wi)
        else:
            if not os.path.exists(wi):
                os.makedirs(wi)
            indx = whoosh.index.create_in(wi, schema)

        app.whoosh_indexes[model.__class__.__name__] = indx
        searcher = _Searcher(primary, indx)
        model.__class__.query = _QueryProxy(model.__class__.query, primary,
                searcher, model)

        model.__class__.pure_whoosh = searcher

    return indx


def _get_whoosh_schema_and_primary(model):
    schema = {}
    primary = None
    for field in model.__table__.columns:
        if field.primary_key:
            schema[field.name] = whoosh.fields.ID(stored=True, unique=True)
            primary = field.name
        if field.name in model.__searchable__:
            if isinstance(field.type, (sqlalchemy.types.Text,
                sqlalchemy.types.String, sqlalchemy.types.Unicode)):

                schema[field.name] = whoosh.fields.TEXT(
                        analyzer=StemmingAnalyzer())

    return Schema(**schema), primary


def _after_flush(app, changes):
    ''' Any db updates go through here. We check if any of these models have
    ``__searchable__`` fields, indicating they need to be indexed. With these
    we update the whoosh index for the model. If no index exists, it will be
    created here; this could impose a penalty on the initial commit of a model.
    '''

    bytype = {}  # sort changes by type so we can use per-model writer
    for change in changes:
        update = change[1] in ('update', 'insert')

        if hasattr(change[0].__class__, '__searchable__'):
            bytype.setdefault(change[0].__class__.__name__, []).append((update,
                change[0]))

    for model, values in bytype.iteritems():
        index = _whoosh_index(app, values[0][1])
        with index.writer() as writer:
            primary_field = values[0][1].pure_whoosh.primary_key_name
            searchable = values[0][1].__searchable__

            for update, v in values:
                if update:
                    attrs = {}
                    for key in searchable:
                        try:
                            attrs[key] = unicode(getattr(v, key))
                        except AttributeError:
                            raise AttributeError(
                                    '%s does not have __searchable__ field %s'
                                    % (model, key))

                    attrs[primary_field] = unicode(getattr(v, primary_field))
                    writer.update_document(**attrs)
                else:
                    writer.delete_by_term(primary_field, unicode(getattr(v,
                        primary_field)))


models_committed.connect(_after_flush)
