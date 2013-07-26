#
# Copyright (c) 2013 Hewlett-Packard Development Company, L.P.
#
# Confidential computer software. Valid license from HP required for
# possession, use or copying. Consistent with FAR 12.211 and 12.212,
# Commercial Computer Software, Computer Software Documentation, and
# Technical Data for Commercial Items are licensed to the U.S. Government
# under vendor's standard commercial license.
#

from ghp.lib.utils import GitMixin
from ghp.log import LogDedentMixin

from abc import ABCMeta, abstractmethod
from git.commit import Commit


class Searcher(GitMixin):
    """
    Base class that needs to be extended with the specific searcher on how to
    locate changes.
    """
    __metaclass__ = ABCMeta

    def __init__(self, branch="HEAD", *args, **kwargs):

        self._branch = branch

        self.filters = []
        self.commit = None

        super(Searcher, self).__init__(*args, **kwargs)

    @property
    def branch(self):
        """
        Default branch in the git repository to search.
        """
        return self._branch

    def addFilter(self, filter):
        if filter not in self.filters:
            self.filters.append(filter)

    @abstractmethod
    def find(self):
        """
        Implementation of this method should return a commit SHA1 that will be
        used by the generic list() method as the start commit to list commits
        from, to the tip of the branch or commit given in the constructor.

        In additional it must save a 'Commit' object for the found SHA1 as the
        self.commit property.
        """
        pass

    def list(self):
        """
        Returns a list of Commit objects, between the '<commitish>' revision
        given in the constructor, and the commit object returned by the find()
        method.
        """
        if not self.commit:
            self.find()

        # walk the tree and find all commits that lie in the path between the
        # commit found by find() and head of the branch to provide a list of
        # commits to the caller
        self.log.info(
            """\
            Walking the ancestry path between found commit and target
                git rev-list --parents --ancestry-path %s..%s
            """, self.commit.id, self.branch)

        # depending on how many commits are returned, may need to examine use
        # of a generator rather than storing the entire list. Such a generator
        # would require a custom git object or direct use of popen
        commit_list = Commit.find_all(self.repo,
                                      "{0}..{1}".format(self.commit.id,
                                                        self.branch),
                                      topo_order=True, ancestry_path=True)

        # chain the filters as generators so that we don't need to allocate new
        # lists for each step in the filter chain.
        for f in self.filters:
            commit_list = f.filter(commit_list)

        commits = list(commit_list)

        self.log.debug(
            """\
            commits found:
                %s
            """, ("\n" + " " * 4).join([c.id for c in commits]))

        return commits


class NullSearcher(Searcher):
    """
    This searcher returns an empty list
    """

    def list(self):
        return []


class UpstreamMergeBaseSearcher(LogDedentMixin, Searcher):
    """
    Searches upstream references for a merge base with the target branch. By
    default this will search the 'upstream/*' namespace but can be overridden
    to search any namespace pattern.

    If not restricted to search specific remotes, it will search all
    available remote references matching the patern for the most recent merge
    base available.
    """

    def __init__(self, pattern="upstream/*", search_tags=False,
                 remotes=[], *args, **kwargs):

        self._pattern = pattern
        self._references = ["refs/heads/{0}".format(self.pattern)]

        super(UpstreamMergeBaseSearcher, self).__init__(*args, **kwargs)

        if remotes:
            self._references.extend(
                ["refs/remotes/{0}/{1}".format(s, self.pattern)
                 for s in remotes])
        else:
            self._references.append(
                "refs/remotes/*/{0}".format(self.pattern))

        if search_tags:
            self._references.append("refs/tags/{0}".format(self.pattern))

    @property
    def pattern(self):
        """
        Pattern to limit which references are searched when looking for a
        merge base commit.
        """
        return self._pattern

    def find(self):
        """
        Searches the git history including local and remote branches, and tags
        if tag searching is enabled. References are included in the list to be
        checked if they match the pattern that was specified in the constructor.
        While 'git rev-list' supports a glob option to check all references, it
        isn't possible to anchor the pattern, so 'upstream/*' would match all
        of the following:
            refs/remotes/origin/upstream/master
            refs/heads/upstream/master
            refs/remotes/origin/other/upstream/area <--- undesirable

        Additional since 'git rev-list' doesn't accept patterns as commit refs
        it's better to make use of 'git for-each-ref' and how it does pattern
        matching in order to generate a list of refernces to pass to rev-list
        to walk.

        After determining all the references to look at, because of the
        overhead in using 'git merge-base' to determine the last commit from
        one of the upstream refs that was merged into the target branch, it is
        worth going to the additional effort of removing any reference that is
        reachable from another so that the calls to merge-base are minimized.
        """

        self.log.info("Searching for most recent merge base with upstream branches")

        rev_list_args = []

        # process pattern given to get a list of refs to check
        rev_list_args = self.git.for_each_ref(*self._references,
                                              format="%(refname:short)"
                                              ).splitlines()
        self.log.info(
            """\
            Upstream refs:
                %s
            """, "\n    ".join(rev_list_args)
        )

        # get the sha1 for the tip of each of the upstream refs we are going to search
        self.log.info(
            """\
            Construct list of upstream revs to search:
                git rev-list --min-parents=1 --no-walk \\
                    %s
            """, (" \\\n" + " " * 8).join(rev_list_args))
        search_list = set(self.git.rev_list(*rev_list_args,
                                            min_parents=1,
                                            no_walk=True).splitlines())
        rev_list_args = list(search_list)

        # construct a list of the parents of each ref so that we can tell
        # rev-list to ignore in the anything reachable from the list commits
        # which reduces the amount of revs to be searched with merge-base
        prune_list = []
        for rev in search_list:
            # only root commits won't have at least one parent which have been
            # excluded by the previous search
            commit = self.git.rev_list(rev, parents=True, max_count=1).split()
            parents = commit[1:]
            prune_list.extend(parents)

        # We want to stop walking the tree and ignore all commits after each
        # time we encounter one from the prune_list, so make sure to set the
        # --not option followed by the list of revisions to exclude. Since
        # python-git may reorder options if given by way or keyword args, use
        # strings in the required order as '*args' are not reordered, and
        # order is critical here to ensure rev-list applies the '--not'
        # behaviour to the correct set.
        rev_list_args.append("--not")
        rev_list_args.extend(prune_list)
        self.log.info(
            """\
            Retrieve minimal list of revs to check with merge-base by excluding
            revisions that are in the reachable from others in the list:
                git rev-list \\
                    %s
            """, " \\\n        ".join(rev_list_args))
        revsions = self.git.rev_list(*rev_list_args).splitlines()

        # Running 'git merge-base' is relatively expensive to pruning the list
        # of revs to search since it needs to construct and walk a large portion
        # of the tree for each call. If the constructed graph was retained
        # betweens we could likely remove much of the code above.
        self.log.info(
            """\
            Running merge-base against each found upstream revision and target
                git merge-base %s ${upstream_rev}
            """, self.branch)
        merge_bases = set()
        for rev in revsions:
            # ignore exceptions as there may be unrelated branches picked up by
            # the searching which would result in merge-base returning an error
            base = self.git.merge_base(self.branch, rev, with_exceptions=False)
            if base:
                merge_bases.add(base)

        self.log.info(
            """\
            Order the possible merge-base commits in descendent order, to
            find the most recent one used irrespective of date:
                git rev-list --topo-order --max-count=1 --no-walk \\
                    %s
            """, (" \\\n" + " " * 8).join(merge_bases))
        sha1 = self.git.rev_list(*merge_bases, topo_order=True, max_count=1,
                                 no_walk=True)
        # now that we have the sha1, make sure to save the commit object
        self.commit = self.repo.commit(sha1)
        self.log.debug("Most recent merge-base commit is: '%s'", self.commit.id)

        if not self.commit:
            raise RuntimeError("Failed to locate suitable merge-base")

        return self.commit.id

    def list(self, include_all=False):
        """
        If a particular commit has been merged in mulitple times, walking the
        ancestry path and returning all results will return all commits from
        each merge point, not just the last set which are usually what is
        desired.

        X --- Y --- Z              - other branch
         \           \
          B --- C --- D --- C'     - HEAD
         /           /
        A --------------- E        - upstream


        When importing "E" from the upstream branch, the previous import commit
        is detected as "A". The commits that we are actually interested in are:

                      D --- C'     - HEAD
                     /
        A -----------              - upstream

        However due to the DAG nature of git, when walking the direct ancestry
        path, it cannot for certain determine which parent of merge commits
        was the original mainline. Thus it will return the following graph.

          B --- C --- D --- C'     - HEAD
         /           /
        A -----------              - upstream

        With the following topological order ABCADC'

        The BeforeFirstParentCommitFilter can be given the "A" commit to use
        when walking the list of commits from the most recent "C'" to the
        earliest and will stop at the first occurance of "A", ignoring all
        other earlier commits.

        Setting the parameter 'include_all' to True, will return the entire
        list.

        if include_all == False -> return ADC'
        if include_all == True -> return ABCADC'
        """

        if not include_all:
            self.filters.insert(0, BeforeFirstParentCommitFilter(self.find()))

        return super(UpstreamMergeBaseSearcher, self).list()


class CommitMessageSearcher(LogDedentMixin, Searcher):
    """
    This searcher returns a list of commits based on looking for commit message
    containing a specific message in the current branch.
    """

    def __init__(self, pattern, *args, **kwargs):
        self._pattern = pattern

        super(CommitMessageSearcher, self).__init__(*args, **kwargs)

    @property
    def pattern(self):
        """
        Pattern to search commit messages in the target branch for a match.
        """
        return self._pattern

    def find(self):
        """
        Searches the git history of the target branch for a commit message
        containing the pattern given in the constructor. This is used as a base
        commit from which to return a list of commits since this point.
        """

        commits = Commit.find_all(self.repo, self.branch, grep=self.pattern,
                                  max_count=1, extended_regexp=True)
        if not commits:
            raise RuntimeError("Failed to locate a pattern match")

        self.commit = commits.pop(0)

        self.log.notice("Commit matching search pattern is: '%s'", self.commit.id)

        return self.commit.id

    def list(self, include=True):
        """
        Override parent implementation to permit inclusion of the found commit
        to be returned in the list of changes. This will help in cases where
        then commit is a specific merge including the additionally merged
        branches that would be returned by the generic upstream searcher.
        """

        commits = super(CommitMessageSearcher, self).list()
        if include:
            commits.append(self.commit)

        return commits


class CommitFilter(object):
    """
    CommitFilter instances are used to perform arbitrary filtering of commits
    returned by searchers.
    """
    __metaclass__ = ABCMeta

    def __init__(self, *args, **kwargs):
        super(CommitFilter, self).__init__(*args, **kwargs)

    @abstractmethod
    def filter(self, commit_iter):
        pass


class MergeCommitFilter(CommitFilter):
    """
    Includes only those commits that have more than one parent listed (merges)
    """
    def filter(self, commit_iter):
        for commit in commit_iter:
            if len(commit.parents) >= 2:
                yield commit


class NoMergeCommitFilter(CommitFilter):
    """
    Prunes all that have more than one parent listed (merges)
    """
    def filter(self, commit_iter):
        for commit in commit_iter:
            if len(commit.parents) < 2:
                yield commit


class BeforeFirstParentCommitFilter(LogDedentMixin, CommitFilter):
    """
    Generator that iterates over commit objects until it encounters a one that
    has a parent commit SHA1 that matches the 'stopcommit' SHA1.
    """

    def __init__(self, stopcommit, *args, **kwargs):
        self.stop = stopcommit
        super(BeforeFirstParentCommitFilter, self).__init__(*args, **kwargs)

    def filter(self, commit_iter):
        for commit in commit_iter:
            # return the commit object first before checking if the parent
            # matches the stop commit otherwise we'll also trim the commit
            # before the one we wanted to match as well.
            yield commit
            if any(parent.id == self.stop for parent in commit.parents):
                self.log.debug("Discarding all commits before '%s'",
                               commit.id)
                break


class CommitSHA1Filter(CommitFilter):
    """
    Trim output to the SHA1's of each commit
    """

    def filter(self, commit_iter):
        for commit in commit_iter:
            yield commit.id


class ReverseCommitFilter(LogDedentMixin, CommitFilter):
    """
    Reverses the list of commits passed.

    As this needs the complete list to work, it is recommended to only use
    once all other filtering is complete as otherwise it will remove the
    memory benefits of using generator behaviour when chaining multiple
    filters.
    """

    def filter(self, commit_iter):
        self.log.debug("Comsuming generators to reverse commit list")
        return reversed(list(commit_iter))