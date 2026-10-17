// Count a full beat per cycle. Each bin gets the population count of matching
// lanes, so repeated IDs in the same beat cannot overwrite each other's updates.
module dut #(
    parameter LEN = 256, BINS = 16, LANES = 8, DATA_W = 8, ACC_W = 32
)(
    input wire clk, rst_n,
    input wire [LANES*DATA_W-1:0] in_x_flat,
    input wire in_x_flat_valid,
    output wire in_x_flat_ready,
    output wire out_valid,
    input wire out_ready,
    output wire [ACC_W-1:0] out_c
);
    localparam BEATS = LEN / LANES;
    localparam BW = (BEATS > 1) ? $clog2(BEATS) : 1;
    localparam IW = (BINS > 1) ? $clog2(BINS) : 1;
    localparam CW = $clog2(LEN + 1);
    localparam HW = $clog2(LANES + 1);
    reg sending;
    reg [BW-1:0] beat;
    reg [IW-1:0] emitted;
    reg [CW-1:0] counts [0:BINS-1];
    reg [HW-1:0] hits [0:BINS-1];
    integer b, l, j;
    always @(*) begin
        for (b = 0; b < BINS; b = b + 1) begin
            hits[b] = 0;
            for (l = 0; l < LANES; l = l + 1)
                hits[b] = hits[b] + (in_x_flat[l*DATA_W +: DATA_W] == b);
        end
    end

    assign in_x_flat_ready = !sending;
    assign out_valid = sending;
    assign out_c = counts[emitted];
    always @(posedge clk) begin
        if (!rst_n) begin
            sending <= 1'b0;
            beat <= 0;
            emitted <= 0;
            for (j = 0; j < BINS; j = j + 1) counts[j] <= 0;
        end else if (sending) begin
            if (out_ready) begin
                if (emitted == BINS - 1) begin
                    emitted <= 0;
                    for (j = 0; j < BINS; j = j + 1) counts[j] <= 0;
                    sending <= 1'b0;
                end else emitted <= emitted + 1'b1;
            end
        end else if (in_x_flat_valid) begin
            for (j = 0; j < BINS; j = j + 1) counts[j] <= counts[j] + hits[j];
            if (beat == BEATS - 1) begin
                beat <= 0;
                sending <= 1'b1;
            end else beat <= beat + 1'b1;
        end
    end
endmodule
