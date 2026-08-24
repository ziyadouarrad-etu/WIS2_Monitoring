from django.core.management.base import BaseCommand, CommandError

from telemetry import kb


class Command(BaseCommand):
    help = (
        "Build the local RAG knowledge base for the Explain assistant: "
        "catalogue.html documentation sections plus WCMP2 records harvested "
        "from a WIS2 Global Discovery Catalogue, embedded via Ollama."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--skip-gdc', action='store_true',
            help='Skip harvesting GDC records; index documentation only.',
        )
        parser.add_argument(
            '--gdc-url', default=None,
            help='GDC base URL (defaults to $GDC_API_URL or the ECCC-MSC catalogue).',
        )
        parser.add_argument(
            '--limit', type=int, default=None,
            help='Cap the number of harvested GDC records.',
        )
        parser.add_argument(
            '--delay', type=float, default=0.2,
            help='Seconds between GDC page requests (default 0.2).',
        )
        parser.add_argument(
            '--no-embed', action='store_true',
            help='Write the index without embeddings (keyword retrieval only).',
        )

    def handle(self, *args, **options):
        if options['limit'] is not None and options['limit'] <= 0:
            raise CommandError('--limit must be a positive integer.')

        chunks = kb.doc_chunks()
        self.stdout.write(
            f'Documentation: extracted {len(chunks)} chunk(s) from catalogue.html.'
        )

        if not options['skip_gdc']:
            gdc_chunks = kb.harvest_gdc(
                api_url=options['gdc_url'],
                limit=options['limit'],
                delay=options['delay'],
            )
            self.stdout.write(
                f'GDC: harvested {len(gdc_chunks)} WCMP2 record(s).'
            )
            chunks.extend(gdc_chunks)

        unique = []
        seen = set()
        for chunk in chunks:
            if chunk["id"] in seen:
                continue
            seen.add(chunk["id"])
            unique.append(chunk)
        dropped = len(chunks) - len(unique)
        if dropped:
            self.stdout.write(f'Deduplicated {dropped} duplicate chunk(s).')

        model_used = None
        if options['no_embed']:
            self.stdout.write(self.style.WARNING(
                '--no-embed: writing index without embeddings '
                '(keyword retrieval only).'
            ))
        else:
            model_used = kb.embed_model()
            self.stdout.write(
                f"Embedding {len(unique)} chunk(s) with '{model_used}' ..."
            )
            try:
                vectors = kb.embed_texts([chunk['text'] for chunk in unique])
                for chunk, vector in zip(unique, vectors):
                    chunk['embedding'] = vector
            except Exception as exc:
                model_used = None
                for chunk in unique:
                    chunk.pop('embedding', None)
                self.stdout.write(self.style.WARNING(
                    f'Embedding failed ({exc}); writing index without '
                    'embeddings (keyword retrieval only).'
                ))

        path = kb.save_index(unique, embed_model_name=model_used)
        self.stdout.write(self.style.SUCCESS(
            f'Done: wrote {len(unique)} chunk(s) to {path}'
        ))
