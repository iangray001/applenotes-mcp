// wfimport -- install a signed .shortcut into a Shortcuts library, bypassing the
// Shortcuts app's import-time validation.
//
// Only needed for the FULL bridge build. macOS 27's Shortcuts refuses to import any
// workflow containing com.apple.Notes.SetChecklistItemCheckedLinkActionv2 or
// com.apple.Notes.SetAttachmentSizeLinkAction -- it says only "contains features not
// supported on this device", and the log adds nothing but "Refusing to import shortcut
// with reasons: <private>". The actions are not gone and are not broken: a workflow
// carrying them runs correctly once it is in the library. It is purely the importer that
// objects. See NOTES.md for how that was narrowed down.
//
// This uses PRIVATE WorkflowKit SPI and writes directly to a database that syncs to
// iCloud. Apple can change either at any release. The basic bridge build needs none of
// this -- it installs with a double-click -- so only build this if you want ticked
// checkboxes and attachment display sizes.
//
// The API sequence follows https://github.com/pdfux/generate-shortcut-action-os-27,
// which hit the same import refusal for an unrelated action.
//
// The database path is a REQUIRED argument, deliberately: point it at a copy first.
//
//   clang -fobjc-arc -framework Foundation -o wfimport tools/wfimport.m
//   ./wfimport "~/.local/share/applenotes-mcp/Notes MCP Bridge.shortcut" \
//              ~/Library/Shortcuts/Shortcuts.sqlite
//
// Quit Shortcuts.app first: it caches the library and will not show the new shortcut
// until it is restarted.

#import <Foundation/Foundation.h>
#import <dlfcn.h>

@interface NSObject (WFPrivate)
- (id)initWithSignedShortcutFileURL:(NSURL *)url;
- (id)extractShortcutFileRepresentationWithError:(NSError **)error;
- (id)initWithRecord:(id)record;
- (id)initWithBackgroundColorValue:(long long)color glyphCharacter:(unsigned short)glyph;
- (id)initWithPersistenceMode:(NSUInteger)mode fileURL:(NSURL *)url error:(NSError **)error;
- (id)createWorkflowWithOptions:(id)options error:(NSError **)error;
@end

static void Die(NSString *msg) {
    fprintf(stderr, "wfimport: %s\n", msg.UTF8String);
    exit(1);
}

// WFWorkflowRecord key <- plist key. Anything absent from the plist is simply not set.
static NSDictionary *RecordKeyMap(void) {
    return @{
        @"actions":                     @"WFWorkflowActions",
        @"inputClasses":                @"WFWorkflowInputContentItemClasses",
        @"outputClasses":               @"WFWorkflowOutputContentItemClasses",
        @"workflowTypes":               @"WFWorkflowTypes",
        @"importQuestions":             @"WFWorkflowImportQuestions",
        @"lastMigratedClientVersion":   @"WFWorkflowClientVersion",
        @"hasOutputFallback":           @"WFWorkflowHasOutputFallback",
        @"hasShortcutInputVariables":   @"WFWorkflowHasShortcutInputVariables",
        @"hasOutputAction":             @"WFWorkflowHasOutputAction",
        @"quickActionSurfacesForSharing": @"WFQuickActionSurfaces",
        @"noInputBehavior":             @"WFWorkflowNoInputBehavior",
    };
}

int main(int argc, char **argv) {
    @autoreleasepool {
        @try {
            if (argc != 3) {
                fprintf(stderr, "Usage: %s FILE.shortcut PATH/TO/Shortcuts.sqlite\n", argv[0]);
                return 2;
            }

            if (!dlopen("/System/Library/PrivateFrameworks/WorkflowKit.framework/WorkflowKit",
                        RTLD_LAZY)) {
                Die(@"cannot load WorkflowKit");
            }

            NSString *shortcutPath = [@(argv[1]) stringByExpandingTildeInPath];
            NSString *dbPath = [@(argv[2]) stringByExpandingTildeInPath];
            NSFileManager *fm = NSFileManager.defaultManager;

            if (![fm fileExistsAtPath:shortcutPath]) Die(@"no such .shortcut file");
            if (![fm fileExistsAtPath:dbPath]) Die(@"no such Shortcuts.sqlite");

            NSError *error = nil;

            // 1. Unwrap the signed AEA package back into its workflow plist.
            id package = [[NSClassFromString(@"WFShortcutPackageFile") alloc]
                initWithSignedShortcutFileURL:[NSURL fileURLWithPath:shortcutPath]];
            if (!package) Die(@"WFShortcutPackageFile unavailable");

            id file = [package extractShortcutFileRepresentationWithError:&error];
            if (!file) Die(error.description ?: @"cannot decode the signed shortcut");

            NSData *plistData = [file valueForKey:@"data"];
            NSDictionary *wf = [NSPropertyListSerialization propertyListWithData:plistData
                                                                        options:NSPropertyListImmutable
                                                                         format:NULL
                                                                          error:&error];
            if (![wf isKindOfClass:NSDictionary.class] ||
                ![wf[@"WFWorkflowActions"] isKindOfClass:NSArray.class]) {
                Die(@"decoded file has no action list");
            }

            // 2. Rebuild it as the record type the library stores.
            id record = [[NSClassFromString(@"WFWorkflowRecord") alloc] init];
            if (!record) Die(@"WFWorkflowRecord unavailable");

            NSDictionary *map = RecordKeyMap();
            for (NSString *key in map) {
                id value = wf[map[key]];
                if (value) [record setValue:value forKey:key];
            }

            NSString *name = shortcutPath.lastPathComponent.stringByDeletingPathExtension;
            [record setValue:name forKey:@"name"];
            [record setValue:@([wf[@"WFWorkflowActions"] count]) forKey:@"actionCount"];

            if (wf[@"WFWorkflowMinimumClientVersion"]) {
                [record setValue:[wf[@"WFWorkflowMinimumClientVersion"] description]
                          forKey:@"minimumClientVersion"];
            }

            NSDictionary *icon = wf[@"WFWorkflowIcon"];
            if (icon) {
                id wfIcon = [[NSClassFromString(@"WFWorkflowIcon") alloc]
                    initWithBackgroundColorValue:[icon[@"WFWorkflowIconStartColor"] longLongValue]
                                  glyphCharacter:[icon[@"WFWorkflowIconGlyphNumber"] unsignedShortValue]];
                if (wfIcon) [record setValue:wfIcon forKey:@"icon"];
            }

            // 3. Insert. This is the step the Shortcuts app guards with the validation
            //    that rejects the two Notes actions; going through WFDatabase skips it.
            id options = [[NSClassFromString(@"WFWorkflowCreationOptions") alloc]
                initWithRecord:record];
            if (!options) Die(@"WFWorkflowCreationOptions unavailable");
            [options setValue:@YES forKey:@"addToLibrary"];

            id database = [[NSClassFromString(@"WFDatabase") alloc]
                initWithPersistenceMode:0
                                fileURL:[NSURL fileURLWithPath:dbPath]
                                  error:&error];
            if (!database) Die(error.description ?: @"cannot open the library");

            id reference = [database createWorkflowWithOptions:options error:&error];
            if (!reference) Die(error.description ?: @"cannot create the shortcut record");

            printf("imported %s into %s\n", name.UTF8String, dbPath.UTF8String);
        } @catch (NSException *e) {
            Die(e.reason ?: @"unknown exception");
        }
    }
    return 0;
}
