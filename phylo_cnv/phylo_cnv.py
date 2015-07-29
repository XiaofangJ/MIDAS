#!/usr/bin/python

# PhyloCNV - estimating the abundance, gene-content, and phylogeny of microbes from metagenomes
# Copyright (C) 2015 Stephen Nayfach
# Freely distributed under the GNU General Public License (GPLv3)

__version__ = '0.0.1'

# Libraries
# ---------
import sys
import os
import numpy as np
import argparse
import pysam
import gzip
import time
import subprocess
import operator
import Bio.SeqIO
import phylo_species
import resource
from collections import defaultdict
from math import ceil

# Functions
# ---------

def check_arguments(args):
	""" Check validity of command line arguments """
	
	# Pipeline options
	if not any([args['all'], args['profile'], args['align'], args['map'],
	            args['cov'], args['extract'], args['remap'], args['snps']]):
		sys.exit('Specify one or more pipeline option(s): --all, --profile, --align, --map, --cov, --extract, --remap, --snps')
	if args['all']:
		args['profile'] = True
		args['align'] = True
		args['map'] = True
		args['cov'] = True
		args['extract'] = True
		args['remap'] = True
		args['snps'] = True
	if args['tax_mask'] and not args['tax_map']:
		sys.exit('Specify file mapping read ids in FASTQ file to genome ids in reference database')

	# Input options
	if not args['m1'] and (args['profile'] or args['align']):
		sys.exit('Specify input FASTQ file(s) with -1 -2 or -U')
	if args['m1'] and not os.path.isfile(args['m1']):
		sys.exit('Input file specified with -1 does not exist')
	if args['m2'] and not os.path.isfile(args['m2']):
		sys.exit('Input file specified with -2 does not exist')
	if args['db_dir'] and not os.path.isdir(args['db_dir']):
		sys.exit('Input directory specified with --db-dir does not exist')

	# Output options
	if not args['out']:
		sys.exit('Specify output directory with -o')

def print_copyright():
	# print out copyright information
	print ("-------------------------------------------------------------------------")
	print ("PhyloCNV - estimating the abundance, gene-content, and phylogeny of microbes from metagenomes")
	print ("version %s; github.com/snayfach/PhyloCNV" % __version__)
	print ("Copyright (C) 2015 Stephen Nayfach")
	print ("Freely distributed under the GNU General Public License (GPLv3)")
	print ("-------------------------------------------------------------------------")

def read_phylo_species(inpath):
	""" Parse output from PhyloSpecies """
	if not os.path.isfile(inpath):
		sys.exit("Could not locate species profile: %s\nTry rerunning with --profile" % inpath)
	dict = {}
	fields = [
		('cluster_id', str), ('reads', float), ('bp', float), ('rpkg', float),
		('cov', float), ('prop_cov', float), ('rel_abun', float)]
	infile = open(inpath)
	next(infile)
	for line in infile:
		values = line.rstrip().split()
		dict[values[0]] = {}
		for field, value in zip(fields[1:], values[1:]):
			dict[values[0]][field[0]] = field[1](value)
	return dict

def select_genome_clusters(cluster_abundance, args):
	""" Select genome clusters to map to """
	my_clusters = {}
	# prune all genome clusters that are missing from database
	# this can happen when using an environment specific database
	for cluster_id in cluster_abundance.copy():
		if not os.path.isdir('/'.join([args['db_dir'], cluster_id])):
			del cluster_abundance[cluster_id]
	# user specified a single genome-cluster
	if args['gc_id']:
		cluster_id = args['gc_id']
		if cluster_id not in cluster_abundance:
			sys.exit("Error: specified genome-cluster id %s not found" % cluster_id)
		else:
			abundance = cluster_abundance[args['gc_id']]['rel_abun']
			my_clusters[args['gc_id']] = abundance
	# user specified a list of genome-clusters
	elif args['gc_list']:
		for cluster_id in args['gc_list'].split(','):
			if cluster_id not in cluster_abundance:
				sys.exit("Error: specified genome-cluster id %s not found" % cluster_id)
			else:
				abundance = cluster_abundance[cluster_id]['rel_abun']
				my_clusters[cluster_id] = coverage
	# user specifed a coverage threshold
	elif args['gc_cov']:
		for cluster_id, values in cluster_abundance.items():
			if values['cell_count'] >= args['gc_cov']:
				my_clusters[cluster_id] = values['cov']
	# user specifed a relative-abundance threshold
	elif args['gc_rbun']:
		for cluster_id, values in cluster_abundance.items():
			if values['prop_mapped'] >= args['gc_rbun']:
				my_clusters[cluster_id] = values['rel_abun']
	# user specifed a relative-abundance threshold
	elif args['gc_topn']:
		cluster_abundance = [(i,d['rel_abun']) for i,d in cluster_abundance.items()]
		sorted_abundance = sorted(cluster_abundance, key=operator.itemgetter(1), reverse=True)
		for cluster_id, coverage in sorted_abundance[0:args['gc_topn']]:
			my_clusters[cluster_id] = coverage
	return my_clusters

def align_reads(args, genome_clusters, batch_index, reads_start, batch_size, tax_mask):
	""" Use Bowtie2 to map reads to all specified genome clusters """
	# Create output directory
	outdir = os.path.join(args['out'], 'bam')
	if not os.path.isdir(outdir): os.mkdir(outdir)
	for cluster_id in genome_clusters:
		# Build command
		command = '%s --no-unal ' % args['bowtie2']
		#   index
		command += '-x %s ' % '/'.join([args['db_dir'], cluster_id, 'genome_cluster', cluster_id])
		#   specify reads
		command += '-s %s -u %s ' % (reads_start, batch_size)
		#   speed/sensitivity
		command += '--%s ' % args['align_speed']
		#   threads
		command += '--threads %s ' % args['threads']
		#	report up to 20 hits/read if masking hits
		if tax_mask: command += '-k 20 '
		#   input
		if (args['m1'] and args['m2']): command += '-1 %s -2 %s ' % (args['m1'], args['m2'])
		else: command += '-U %s' % args['m1']
		#   output
		bampath = '/'.join([args['out'], 'bam', '%s.%s.bam ' % (cluster_id, batch_index)])
		command += '| %s view -b - > %s' % (args['samtools'], bampath)
		# Run command
		if args['verbose']: print("    running: %s") % command
		process = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
		out, err = process.communicate()
		#sys.stderr.write(err) # write to stderr: bowtie2 output

def align_to_rep(args, genome_clusters):
	""" Use Bowtie2 to map reads to representative genomes from each genome cluster
	"""
	# Create output directory
	outdir = os.path.join(args['out'], 'bam_rep')
	if not os.path.isdir(outdir): os.mkdir(outdir)
	for cluster_id in genome_clusters:
		# Build command
		#	bowtie2
		command = '%s --no-unal --very-sensitive ' % args['bowtie2']
		#   speed/sensitivity
		command += '--%s ' % args['align_speed']
		#   threads
		command += '--threads %s ' % args['threads']
		#   bt2 index
		command += '-x %s ' % '/'.join([args['db_dir'], cluster_id, 'cluster_centroid', 'centroid'])
		#   input fastq
		command += '-U %s ' % '/'.join([args['out'], 'fastq', '%s.fastq.gz' % cluster_id])
		#   convert to bam
		command += '| %s view -b - ' % args['samtools']
		#   sort bam
		command += '| %s sort -f - %s ' % (args['samtools'], '/'.join([outdir, '%s.bam' % cluster_id]))
		# Run command
		if args['verbose']: print("    running: %s") % command
		process = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
		out, err = process.communicate()

def pileup_on_rep(args, genome_clusters):
	""" Use Samtools to create pileup, filter low quality bases, and write results to VCF file """
	outdir = os.path.join(args['out'], 'vcf')
	if not os.path.isdir(outdir): os.mkdir(outdir)
	for cluster_id in genome_clusters:
		# Build command
		#   mpileup
		command = '%s mpileup -uv -A -d 10000 --skip-indels -B ' % args['samtools']
		#   quality filtering
		command += '-q %s -Q %s ' % (args['snps_mapq'], args['snps_baseq'])
		#   reference fna file
		command += '-f %s ' % '/'.join([args['db_dir'], cluster_id, 'cluster_centroid', 'centroid.fna'])
		#   input bam file
		command += '%s ' % '/'.join([args['out'], 'bam_rep', '%s.bam' % cluster_id])
		#   output vcf file
		command += '> %s ' % '/'.join([outdir, '%s.vcf' % cluster_id])
		# Run command
		if args['verbose']: print("    running: %s") % command
		process = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
		out, err = process.communicate()
		
def format_vcf(args, genome_clusters):
	""" Format vcf output for easy parsing """
	# create outdir
	outdir = '/'.join([args['out'], 'snps'])
	if not os.path.isdir(outdir): os.mkdir(outdir)
	for cluster_id in genome_clusters:
		inpath = '/'.join([args['out'], 'vcf', '%s.vcf' % cluster_id])
		outpath = '/'.join([args['out'], 'snps', '%s.snps.gz' % cluster_id])
		vcf_to_snps(inpath, outpath)

def fetch_reads(aln_file):
	""" Use pysam to yield paired end reads from bam file """
	pe_read = []
	for aln in aln_file.fetch(until_eof = True):
		if not aln.is_paired:
			yield [aln]
		elif aln.mate_is_unmapped and aln.is_read1:
			yield [aln]
		elif aln.mate_is_unmapped and aln.is_read2:
			yield [aln]
		else:
			pe_read.append(aln)
			if len(pe_read) == 2:
				yield pe_read
				pe_read = []

def compute_aln_score(pe_read):
	""" Compute alignment score for single or paired-end read """
	if not pe_read[0].is_paired:
		score = pe_read[0].query_length - dict(pe_read[0].tags)['NM']
		return score
	elif pe_read[0].mate_is_unmapped:
		score = pe_read[0].query_length - dict(pe_read[0].tags)['NM']
		return score
	else:
		score1 = pe_read[0].query_length - dict(pe_read[0].tags)['NM']
		score2 = pe_read[1].query_length - dict(pe_read[1].tags)['NM']
		return score1 + score2

def compute_perc_id(pe_read):
	""" Compute percent identity for paired-end read """
	if not pe_read[0].is_paired:
		length = pe_read[0].query_length
		edit = dict(pe_read[0].tags)['NM']
	elif pe_read[0].mate_is_unmapped:
		length = pe_read[0].query_length
		edit = dict(pe_read[0].tags)['NM']
	else:
		length = pe_read[0].query_length + pe_read[1].query_length
		edit = dict(pe_read[0].tags)['NM'] + dict(pe_read[1].tags)['NM']
	return 100 * (length - edit)/float(length)

def find_best_hits(args, genome_clusters, batch_index, tax_mask, tax_map):
	""" Find top scoring alignment(s) for each read """
	if args['verbose']: print("    finding best alignments across GCs")
	best_hits = {}
	reference_map = {} # (cluster_id, ref_index) = ref_id (ref_id == scaffold id)
	
	# map reads across genome clusters
	for cluster_id in genome_clusters:
	
		# if masking alignments, read in:
		if tax_mask:
			scaffold_to_genome = {} # 1) map of scaffold to genome id
			inpath = '/'.join([args['db_dir'], cluster_id, 'genome_to_scaffold.gz'])
			infile = gzip.open(inpath)
			for line in infile:
				genome_id, scaffold_id = line.rstrip().split('\t')
				scaffold_to_genome[scaffold_id] = genome_id
			run_to_genome = {} # 2) run_accession to genome id
			for line in open(tax_map):
				run_accession, genome_id = line.rstrip().split('\t')
				run_to_genome[run_accession] = genome_id
		
		# get path to bam file
		bam_path = '/'.join([args['out'], 'bam', '%s.%s.bam' % (cluster_id, batch_index)])
		if not os.path.isfile(bam_path):
			sys.stderr.write("      warning: bam file not found for %s.%s" % (cluster_id, batch_index))
			continue
			
		# loop over PE reads
		aln_file = pysam.AlignmentFile(bam_path, "rb")
		for pe_read in fetch_reads(aln_file):
		
			# map reference ids
			for aln in pe_read:
				ref_index = aln.reference_id
				ref_id = aln_file.getrname(ref_index).split('|')[1] # reformat ref id
				reference_map[(cluster_id, ref_index)] = ref_id
				
			# mask alignment
			if tax_mask:
				ref_index = pe_read[0].reference_id
				ref_id = aln_file.getrname(ref_index).split('|')[1]
				run_accession = pe_read[0].query_name.split('.')[0]
				if run_to_genome[run_accession] == scaffold_to_genome[ref_id]:
					continue
					
			# parse pe_read
			query = pe_read[0].query_name
			score = compute_aln_score(pe_read)
			pid = compute_perc_id(pe_read)
			if pid < args['pid']: # filter aln
				continue
			elif query not in best_hits: # store aln
				best_hits[query] = {'score':score, 'aln':{cluster_id:pe_read} }
			elif score > best_hits[query]['score']: # update aln
				best_hits[query] = {'score':score, 'aln':{cluster_id:pe_read} }
			elif score == best_hits[query]['score']: # append aln
				best_hits[query]['aln'][cluster_id] = pe_read
				
	# resolve ties
	best_hits = resolve_ties(args, best_hits, genome_clusters)
	return best_hits, reference_map

def report_mapping_summary(best_hits):
	""" Summarize hits to genome-clusters """
	hit1, hit2, hit3 = 0, 0, 0
	for value in best_hits.values():
		if len(value['aln']) == 1: hit1 += 1
		elif len(value['aln']) == 2: hit2 += 1
		else: hit3 += 1
	if args['reads_align']:
		print("  summary:")
		print("    %s reads assigned to any GC (%s)" % (hit1+hit2+hit3, round(float(hit1+hit2+hit3)/args['reads_align'], 2)) )
		print("    %s reads assigned to 1 GC (%s)" % (hit1, round(float(hit1)/args['reads_align'], 2)) )
		print("    %s reads assigned to 2 GCs (%s)" % (hit2, round(float(hit2)/args['reads_align'], 2)) )
		print("    %s reads assigned to 3 or more GCs (%s)" % (hit3, round(float(hit3)/args['reads_align'], 2)) )
	else:
		print("  summary:")
		print("    %s reads assigned to any GC" % (hit1+hit2+hit3))
		print("    %s reads assigned to 1 GC" % (hit1))
		print("    %s reads assigned to 2 GCs" % (hit2))
		print("    %s reads assigned to 3 or more GCs" % (hit3))

def resolve_ties(args, best_hits, cluster_to_abun):
	""" Reassign reads that map equally well to >1 genome cluster """
	if args['verbose']: print("    reassigning reads mapped to >1 GC")
	for query, rec in best_hits.items():
		if len(rec['aln']) == 1:
			best_hits[query] = rec['aln'].items()[0]
		if len(rec['aln']) > 1:
			target_gcs = rec['aln'].keys()
			abunds = [cluster_to_abun[gc] for gc in target_gcs]
			probs = [abund/sum(abunds) for abund in abunds]
			selected_gc = np.random.choice(target_gcs, 1, p=probs)[0]
			best_hits[query] = (selected_gc, rec['aln'][selected_gc])
	return best_hits

def write_best_hits(args, genome_clusters, best_hits, reference_map, batch_index):
	""" Write reassigned PE reads to disk """
	if args['verbose']: print("    writing mapped reads to disk")
	try: os.makedirs('/'.join([args['out'], 'reassigned']))
	except: pass
	# open filehandles
	aln_files = {}
	scaffold_to_genome = {}
	# loop over genome clusters
	for cluster_id in genome_clusters:
		# get template bam file
		bam_path = '/'.join([args['out'], 'bam', '%s.%s.bam' % (cluster_id, batch_index)])
		if not os.path.isfile(bam_path):
			sys.stderr.write("    bam file not found for %s.%s Skipping\n" % (cluster_id, batch_index))
			continue
		template = pysam.AlignmentFile(bam_path, 'rb')
		# store filehandle
		outpath = '/'.join([args['out'], 'reassigned', '%s.%s.bam' % (cluster_id, batch_index)])
		aln_files[cluster_id] = pysam.AlignmentFile(outpath, 'wb', template=template)
	# write reads to disk
	for cluster_id, pe_read in best_hits.values():
		for aln in pe_read:
			aln_files[cluster_id].write(aln)

def write_pangene_coverage(args, pangene_to_cov, phyeco_cov, cluster_id):
	""" Write coverage of pangenes for genome cluster to disk """
	outdir = '/'.join([args['out'], 'coverage'])
	try: os.mkdir(outdir)
	except: pass
	outfile = gzip.open('/'.join([outdir, '%s.cov.gz' % cluster_id]), 'w')
	for pangene in sorted(pangene_to_cov.keys()):
		cov = pangene_to_cov[pangene]
		cn = cov/phyeco_cov if phyeco_cov > 0 else 0
		outfile.write('\t'.join([pangene, str(cov), str(cn)])+'\n')

def parse_bed_cov(bedcov_out):
	""" Yield dictionary of formatted values from bed coverage output """
	fields =  ['sid', 'start', 'end', 'gene_id', 'pangene_id', 'reads', 'pos_cov', 'gene_length', 'fract_cov']
	formats = [str, int, int, str, str, int, int, int, float]
	for line in bedcov_out.rstrip().split('\n'):
		rec = line.split()
		yield dict([(fields[i],formats[i](j)) for i,j in enumerate(rec)])

def compute_pangenome_coverage(args, cluster_id, batch_index, read_length, pangene_to_cov):
	""" Use bedtools to compute coverage of pangenome """
	bedcov_out = run_bed_coverage(args, cluster_id, batch_index) # run bedtools
	for r in parse_bed_cov(bedcov_out): # aggregate coverage by pangene_id
		pangene_id = r['pangene_id']
		coverage = r['reads'] * read_length / r['gene_length']
		pangene_to_cov[pangene_id] += coverage

def run_bed_coverage(args, cluster_id, batch_index):
	""" Run bedCoverage for cluster_id """
	bampath = '/'.join([args['out'], 'reassigned', '%s.%s.bam' % (cluster_id, batch_index)])
	bedpath = '/'.join([args['db_dir'], cluster_id, 'gene_to_pangene.bed'])
	cmdargs = {'bedcov':args['bedcov'], 'bam':bampath, 'bed':bedpath}
	command = '%(bedcov)s -abam %(bam)s -b %(bed)s' % cmdargs
	process = subprocess.Popen(command % cmdargs, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
	out, err = process.communicate()
	#sys.stderr.write(err) # write to stderr: bedtools output
	return out

def compute_phyeco_cov(args, pangene_to_cov, cluster_id):
	""" Compute coverage of phyeco markers for genome cluster """
	markers = ['B000039','B000041','B000062','B000063','B000065','B000071','B000079',
			   'B000080','B000081','B000082','B000086','B000096','B000103','B000114']
	phyeco_covs = []
	inpath = '/'.join([args['db_dir'], cluster_id, 'pangene_to_phyeco.gz'])
	infile = gzip.open(inpath)
	next(infile)
	for line in infile:
		pangene, phyeco_id = line.rstrip().split()
		if phyeco_id in markers:
			phyeco_covs.append(pangene_to_cov[pangene])
	return np.median(phyeco_covs)

def max_mem_usage():
	""" Return max mem usage (Gb) of self and child processes """
	max_mem_self = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
	max_mem_child = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
	return round((max_mem_self + max_mem_child)/float(1e6), 2)

def get_read_length(args):
	""" Estimate the average read length of fastq file from bam file """
	max_reads = 50000
	read_lengths = []
	bam_dir = '/'.join([args['out'], 'bam'])
	for file in os.listdir(bam_dir):
		bam_path = '/'.join([bam_dir, file])
		aln_file = pysam.AlignmentFile(bam_path, "rb")
		for index, aln in enumerate(aln_file.fetch(until_eof = True)):
			if index == max_reads: break
			else: read_lengths.append(aln.query_length)
	return np.mean(read_lengths)

def get_read_count(inpath):
	""" Count the number of reads in fastq file """
	line_count = 0
	infile = gzip.open(inpath)
	for line_count, line in enumerate(infile):
		pass
	return (line_count+1)/4

def batch_reads(args, batch_size):
	""" Define batches of reads (batch_index, reads_start, reads_end)"""
	batches = []
	total_reads = args['reads_align'] if args['reads_align'] else get_read_count(args['m1'])
	nbatches = int(ceil(total_reads/float(batch_size)))
	for batch_index in range(1, nbatches+1):
		reads_start = (batch_index * batch_size) - batch_size + 1
		reads_end = reads_start + batch_size - 1
		if reads_end > total_reads: # adjust size for final chunk
			batch_size = batch_size - (reads_end - total_reads)
		batches.append([batch_index, reads_start, batch_size])
	return batches

def convert_to_ascii_quality(scores):
	""" Convert quality scores to Sanger encoded (Phred+33) ascii values """
	ascii = """!"#$%&'()*+,-./0123456789:;<=>?@ABCDEFGHIJKLMNOPQR"""
	score_to_ascii = dict((x,y) for x,y in zip(range(0,50),list(ascii)))
	return ''.join([score_to_ascii[x] for x in scores])

def convert_from_ascii_quality(asciis):
	""" Convert quality scores to Sanger encoded (Phred+33) ascii values """
	ascii = """!"#$%&'()*+,-./0123456789:;<=>?@ABCDEFGHIJKLMNOPQR"""
	ascii_to_score = dict((y,x) for x,y in zip(range(0,50),list(ascii)))
	return [ascii_to_score[x] for x in asciis]

def write_fastq_record(aln, index, outfile):
	""" Write pysam alignment record to outfile in FASTQ format """
	outfile.write('@%s.%s length=%s\n' % (aln.query_name,str(index),str(aln.query_length)))
	outfile.write('%s\n' % (aln.query_sequence))
	outfile.write('+%s.%s length=%s\n' % (aln.query_name,str(index),str(aln.query_length)))
	outfile.write('%s\n' % convert_to_ascii_quality(aln.query_qualities))

def bam_to_fastq(genome_clusters, args):
	""" Converts bam to fastq for reads assigned to each genome cluster """
	bam_dir = '/'.join([args['out'], 'reassigned'])
	batch_indexes = sorted(set([_.split('.')[1] for _ in os.listdir(bam_dir)]))
	fastq_dir = '/'.join([args['out'], 'fastq'])
	try: os.mkdir(fastq_dir)
	except: pass
	for genome_cluster in genome_clusters:
		outfile = gzip.open(os.path.join(fastq_dir, genome_cluster+'.fastq.gz'), 'w')
		for batch_index in batch_indexes:
			bam_name = '.'.join([genome_cluster, batch_index, 'bam'])
			bam_path = '/'.join([bam_dir, bam_name])
			aln_file = pysam.AlignmentFile(bam_path, "rb")
			for index, aln in enumerate(aln_file.fetch(until_eof = True)):
				write_fastq_record(aln, index, outfile)

def vcf_to_snps(inpath, outpath):
	""" Parse vcf file in order to call consensus alleles and reference allele frequencies """
	# open outfile
	outfile = gzip.open(outpath, 'w')
	header = ['ref_id', 'ref_pos', 'ref_allele', 'alt_allele', 'cons_allele',
			  'count_alleles', 'count_ref', 'count_alt', 'depth', 'ref_freq']
	outfile.write('\t'.join(header)+'\n')
	# parse vcf
	for r in parse_vcf(inpath):
		rec = [r['ref_id'], r['ref_pos'], r['ref_allele'], r['alt_allele'], r['cons_allele'],
			   r['count_alleles'], r['count_ref'], r['count_alt'], r['depth'], r['ref_freq']]
		outfile.write('\t'.join([str(x) for x in rec])+'\n')

def parse_vcf(inpath):
	""" Yields formatted records from VCF output """
	infile = open(inpath)
	for line in infile:
		# skip header and split line
		if line[0] == '#': continue
		r = line.rstrip().split()
		# get alt alleles
		alt_alleles = r[4].split(',')
		if '<X>' in alt_alleles: alt_alleles.remove('<X>')
		count_alleles = 1 + len(alt_alleles)
		# get allele counts
		info = dict([(_.split('=')) for _ in r[7].split(';')])
		counts = [int(_) for _ in info['I16'].split(',')[0:4]]
		# get consensus allele
		# *note: occassionally there are counts for alternate alleles, but no listed alternate alleles
		if sum(counts) == 0:
			cons_allele = 'NA'
		elif sum(counts[0:2]) >= sum(counts[2:4]):
			cons_allele = r[3]
		elif len(alt_alleles) == 0:
			cons_allele = 'NA'
		else:
			cons_allele = alt_alleles[0]
		# yield formatted record
		yield {'ref_id':r[0],
			   'ref_pos':r[1],
			   'ref_allele':r[3],
			   'count_alleles':count_alleles,
			   'alt_allele':alt_alleles[0] if count_alleles > 1 else 'NA',
			   'depth':sum(counts),
			   'count_ref':sum(counts[0:2]),
			   'count_alt':sum(counts[2:4]),
			   'cons_allele':cons_allele,
			   'ref_freq':'NA'if sum(counts) == 0 else sum(counts[0:2])/float(sum(counts))
			   }

def run_pipeline(args):
	""" Run entire pipeline """
	check_arguments(args) # need to check gc args
	
	main_dir = os.path.dirname(os.path.abspath(__file__))
	args['bowtie2'] = '/'.join([main_dir, 'bin', 'bowtie2'])
	args['samtools'] = '/'.join([main_dir, 'bin', 'samtools'])
	args['bedcov'] = '/'.join([main_dir, 'bin', 'coverageBed'])
	
	if args['verbose']: print_copyright()

	if args['profile']:
		start = time.time()
		if args['verbose']: print("\nEstimating the abundance of genome-clusters")
		cluster_abundance, cluster_summary = phylo_species.estimate_species_abundance(
			{'inpath':args['m1'], 'nreads':args['reads_ms'],
			 'outpath':'/'.join([args['out'], 'genome_clusters']),
			 'min_quality': 25, 'min_length': 50, 'max_n':0.05,
			 'threads':args['threads']})
		phylo_species.write_abundance('%s/genome_clusters.abundance' % args['out'], cluster_abundance)
		phylo_species.write_summary('%s/genome_clusters.summary' % args['out'], cluster_summary)
		if args['verbose']:
			print("  %s minutes" % round((time.time() - start)/60, 2) )
			print("  %s Gb maximum memory") % max_mem_usage()

	if args['verbose']: print("\nSelecting genome-clusters for pangenome alignment")
	cluster_abundance = read_phylo_species('/'.join([args['out'], 'genome_clusters.abundance']))
	genome_clusters = select_genome_clusters(cluster_abundance, args)
	if len(genome_clusters) == 0:
		sys.exit("No genome-clusters were detected")
	elif args['verbose']:
		for cluster, abundance in sorted(genome_clusters.items(), key=operator.itemgetter(1), reverse=True):
			print("  cluster_id: %s abundance: %s" % (cluster, round(abundance,2)))

	if args['align']:
		start = time.time()
		if args['verbose']: print("\nAligning reads to reference genomes")
		for batch_index, reads_start, batch_size in batch_reads(args, args['rd_batch']):
			if args['verbose']: print("  batch %s:" % batch_index)
			align_reads(args, genome_clusters, batch_index, reads_start, batch_size, args['tax_mask'])
		if args['verbose']:
			print("  %s minutes" % round((time.time() - start)/60, 2) )
			print("  %s Gb maximum memory") % max_mem_usage()

	# TO DO:
	# use multithreading to process each batch independently
	if args['map']:
		start = time.time()
		if args['verbose']: print("\nMapping reads to genome clusters")
		# get batch indexes from bam directory
		bam_dir = '/'.join([args['out'], 'bam'])
		batch_indexes = sorted(set([_.split('.')[1] for _ in os.listdir(bam_dir)]))
		# loop over batch indexes
		for batch_index in batch_indexes:
			if args['verbose']: print("  batch %s:" % batch_index)
			best_hits, reference_map = find_best_hits(args, genome_clusters, batch_index, args['tax_mask'], args['tax_map'])
			write_best_hits(args, genome_clusters, best_hits, reference_map, batch_index)
		if args['verbose']:
			print("  %s minutes" % round((time.time() - start)/60, 2) )
			print("  %s Gb maximum memory") % max_mem_usage()

	# TO DO:
	# use multithreading to process each batch independently
	if args['cov']:
		start = time.time()
		if args['verbose']: print("\nEstimating coverage of pangenomes")
		# estimate average read length
		read_length = get_read_length(args)
		# get batch indexes from reassigned directory
		mapped_dir = '/'.join([args['out'], 'reassigned'])
		batch_indexes = sorted(set([_.split('.')[1] for _ in os.listdir(mapped_dir)]))
		# loop over genome-clusters
		for cluster_id in genome_clusters:
			pangene_to_cov = defaultdict(float)
			if args['verbose']: print("  genome-cluster %s" % cluster_id)
			for batch_index in batch_indexes:
				compute_pangenome_coverage(args, cluster_id, batch_index, read_length, pangene_to_cov)
			phyeco_cov = compute_phyeco_cov(args, pangene_to_cov, cluster_id)
			write_pangene_coverage(args, pangene_to_cov, phyeco_cov, cluster_id)
		if args['verbose']:
			print("  %s minutes" % round((time.time() - start)/60, 2) )
			print("  %s Gb maximum memory") % max_mem_usage()

	if args['extract']:
		start = time.time()
		if args['verbose']: print("\nExtacting/writing mapped reads to FASTQ")
		bam_to_fastq(genome_clusters, args)
		if args['verbose']:
			print("  %s minutes" % round((time.time() - start)/60, 2) )
			print("  %s Gb maximum memory") % max_mem_usage()

	if args['remap']:
		if args['verbose']: print("\nMapping reads to rep-genomes")
		start = time.time()
		align_to_rep(args, genome_clusters)
		if args['verbose']:
			print("  %s minutes" % round((time.time() - start)/60, 2) )
			print("  %s Gb maximum memory") % max_mem_usage()

	if args['snps']:
		start = time.time()
		if args['verbose']: print("\nEstimating allele frequencies from mpileup")
		pileup_on_rep(args, genome_clusters)
		format_vcf(args, genome_clusters)
		if args['verbose']:
			print("  %s minutes" % round((time.time() - start)/60, 2) )
			print("  %s Gb maximum memory") % max_mem_usage()
